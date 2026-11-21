"""The volume restore must validate before it changes anything, and undo a failed copy."""
import importlib.util
import io
import sqlite3
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("volume_restore", ROOT / "scripts/volume_restore.py")
volume_restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(volume_restore)


def database(path: Path, marker: str) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE note (text TEXT)")
    connection.execute("INSERT INTO note VALUES (?)", (marker,))
    connection.execute("PRAGMA user_version = 4")
    connection.commit()
    connection.close()


def marker(data: Path) -> str:
    connection = sqlite3.connect(data / "audit.sqlite3")
    try:
        return connection.execute("SELECT text FROM note").fetchone()[0]
    finally:
        connection.close()


def archive_of(source: Path, target: Path) -> Path:
    with tarfile.open(target, "w:gz") as tar:
        for entry in source.iterdir():
            tar.add(entry, arcname=entry.name)
    return target


@pytest.fixture
def volume(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    database(data / "audit.sqlite3", "current")
    (data / "keep-me.txt").write_text("current sidecar")
    return data


def test_good_backup_replaces_everything(tmp_path, volume):
    backup = tmp_path / "backup"
    backup.mkdir()
    database(backup / "audit.sqlite3", "restored")
    archive = archive_of(backup, tmp_path / "good.tar.gz")
    assert volume_restore.main(["x", str(archive), str(volume)]) == 0
    assert marker(volume) == "restored"
    assert not (volume / "keep-me.txt").exists()
    assert not (volume / volume_restore.ASIDE).exists()


@pytest.mark.parametrize("damage", ["truncated", "corrupt-db", "empty-db", "no-db", "no-version", "traversal"])
def test_bad_backup_leaves_the_volume_unchanged(tmp_path, volume, damage):
    backup = tmp_path / "backup"
    backup.mkdir()
    if damage == "corrupt-db":
        database(backup / "audit.sqlite3", "restored")
        raw = bytearray((backup / "audit.sqlite3").read_bytes())
        raw[100:4096] = b"\xff" * (4096 - 100)
        (backup / "audit.sqlite3").write_bytes(bytes(raw))
    elif damage == "empty-db":
        (backup / "audit.sqlite3").write_bytes(b"")
    elif damage == "no-db":
        (backup / "other.txt").write_text("fabricated")
    elif damage == "no-version":
        sqlite3.connect(backup / "audit.sqlite3").execute("CREATE TABLE t (x)").connection.close()
    else:
        database(backup / "audit.sqlite3", "restored")
    archive = archive_of(backup, tmp_path / "backup.tar.gz")
    if damage == "truncated":
        archive.write_bytes(archive.read_bytes()[:60])
    if damage == "traversal":
        with tarfile.open(archive, "w:gz") as tar:
            info = tarfile.TarInfo("../escape.txt")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    assert volume_restore.main(["x", str(archive), str(volume)]) == 1
    assert marker(volume) == "current"
    assert (volume / "keep-me.txt").read_text() == "current sidecar"
    assert not (volume / volume_restore.ASIDE).exists()
    assert not (tmp_path / "escape.txt").exists()


def test_failed_copy_puts_the_current_data_back(tmp_path, volume, monkeypatch):
    backup = tmp_path / "backup"
    backup.mkdir()
    database(backup / "audit.sqlite3", "restored")
    archive = archive_of(backup, tmp_path / "good.tar.gz")

    def broken_copy(*_args, **_kwargs):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(volume_restore.shutil, "copy2", broken_copy)
    with pytest.raises(OSError):
        volume_restore.restore(archive, volume)
    assert marker(volume) == "current"
    assert (volume / "keep-me.txt").exists()
    assert not (volume / volume_restore.ASIDE).exists()


def test_failed_move_aside_puts_the_moved_files_back(tmp_path, volume, monkeypatch):
    backup = tmp_path / "backup"
    backup.mkdir()
    database(backup / "audit.sqlite3", "restored")
    archive = archive_of(backup, tmp_path / "good.tar.gz")
    real_rename = Path.rename
    calls = {"n": 0}

    def flaky_rename(self, target):
        calls["n"] += 1
        if calls["n"] == 2:  # the first entry moves, the second fails
            raise OSError("I/O error (simulated)")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    with pytest.raises(OSError):
        volume_restore.restore(archive, volume)
    monkeypatch.setattr(Path, "rename", real_rename)
    assert marker(volume) == "current"
    assert (volume / "keep-me.txt").read_text() == "current sidecar"
    assert not (volume / volume_restore.ASIDE).exists()


@pytest.mark.parametrize("member", [".pre-restore/old.sqlite3", "nested/.pre-restore/x"])
def test_archive_holding_the_reserved_directory_is_refused(tmp_path, volume, member):
    backup = tmp_path / "backup"
    backup.mkdir()
    database(backup / "audit.sqlite3", "restored")
    archive = archive_of(backup, tmp_path / "reserved.tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(backup / "audit.sqlite3", arcname="audit.sqlite3")
        info = tarfile.TarInfo(member)
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    assert volume_restore.main(["x", str(archive), str(volume)]) == 1
    assert marker(volume) == "current"
    assert (volume / "keep-me.txt").exists()
    assert not (volume / volume_restore.ASIDE).exists()


def test_completed_restore_still_succeeds_when_the_old_copy_cannot_be_removed(tmp_path, volume, monkeypatch, capsys):
    backup = tmp_path / "backup"
    backup.mkdir()
    database(backup / "audit.sqlite3", "restored")
    archive = archive_of(backup, tmp_path / "good.tar.gz")
    real_rmtree = volume_restore.shutil.rmtree

    def stuck_rmtree(path, *args, **kwargs):
        if Path(path).name == volume_restore.ASIDE:
            raise PermissionError("read-only (simulated)")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(volume_restore.shutil, "rmtree", stuck_rmtree)
    assert volume_restore.main(["x", str(archive), str(volume)]) == 0
    assert marker(volume) == "restored"
    assert "remove it by hand" in capsys.readouterr().err


def test_leftover_aside_directory_blocks_a_new_restore(tmp_path, volume):
    (volume / volume_restore.ASIDE).mkdir()
    backup = tmp_path / "backup"
    backup.mkdir()
    database(backup / "audit.sqlite3", "restored")
    archive = archive_of(backup, tmp_path / "good.tar.gz")
    assert volume_restore.main(["x", str(archive), str(volume)]) == 1
    assert marker(volume) == "current"

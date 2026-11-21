"""Restore the data volume from a `make backup` archive. Runs inside the API image (`make restore`).

Order matters: the archive is unpacked into a scratch directory and the database inside it must
pass SQLite's integrity check before anything in the data directory changes. The current contents
are then moved aside, not deleted, and are put back if copying the restored files fails. Only a
complete restore removes them. Prints counts only; never prints file contents.

Usage: python volume_restore.py ARCHIVE DATA_DIR
"""
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

DATABASE = "audit.sqlite3"
ASIDE = ".pre-restore"


class RestoreError(Exception):
    """The archive or its database cannot be restored; the data directory is unchanged."""


def unpack(archive: Path, target: Path) -> None:
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                name = Path(member.name)
                if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
                    raise RestoreError("archive holds an unsafe or unsupported entry")
                if ASIDE in name.parts:
                    raise RestoreError(f"archive holds the reserved {ASIDE} directory")
            # Members are checked above; the data filter also refuses links and special files.
            if hasattr(tarfile, "data_filter"):
                tar.extractall(target, filter="data")
            else:
                tar.extractall(target)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise RestoreError(f"archive cannot be read ({type(exc).__name__})") from None


def check_database(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise RestoreError(f"archive has no {DATABASE}")
    try:
        connection = sqlite3.connect(path)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        raise RestoreError(f"database in the archive is unreadable ({type(exc).__name__})") from None
    if result != "ok":
        raise RestoreError("database in the archive fails the integrity check")
    if version < 1:
        raise RestoreError("database in the archive has no schema version")


def restore(archive: Path, data: Path) -> int:
    aside = data / ASIDE
    if aside.exists():
        raise RestoreError(f"{aside} exists from an earlier restore; inspect and remove it first")
    with tempfile.TemporaryDirectory() as scratch:
        staged = Path(scratch) / "restore"
        staged.mkdir()
        unpack(archive, staged)
        check_database(staged / DATABASE)
        current = [entry for entry in data.iterdir() if entry.name != ASIDE]
        restored = list(staged.iterdir())
        if any(entry.name == ASIDE for entry in restored):  # unpack() refuses it; never copy onto it
            raise RestoreError(f"archive holds the reserved {ASIDE} directory")
        moved: list[str] = []
        copied: list[Path] = []
        aside.mkdir()
        try:
            for entry in current:
                entry.rename(aside / entry.name)
                moved.append(entry.name)
            for entry in restored:
                target = data / entry.name
                copied.append(target)
                if entry.is_dir():
                    shutil.copytree(entry, target)
                else:
                    shutil.copy2(entry, target)
        except BaseException:
            # Undo exactly what happened: drop what was copied in, move back what was moved.
            for target in copied:
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.exists():
                    target.unlink()
            for name in moved:
                (aside / name).rename(data / name)
            aside.rmdir()
            raise
    # The restore is complete here. Failing to delete the old copy does not undo it.
    try:
        shutil.rmtree(aside)
    except OSError:
        print(f"restored, but the previous data could not be removed from {aside}; "
              "remove it by hand before the next restore", file=sys.stderr)
    return len(restored)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: volume_restore.py ARCHIVE DATA_DIR", file=sys.stderr)
        return 2
    try:
        count = restore(Path(argv[1]), Path(argv[2]))
    except RestoreError as exc:
        print(f"restore refused: {exc}; the data directory is unchanged", file=sys.stderr)
        return 1
    print(f"restored {count} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

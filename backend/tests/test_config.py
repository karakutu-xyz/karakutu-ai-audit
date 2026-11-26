import pytest
from audit.config import Settings


def test_configured_token_requires_at_least_32_characters():
    with pytest.raises(ValueError, match="32"):
        Settings(api_auth_token="short")
    assert Settings(api_auth_token="a" * 32).api_auth_token == "a" * 32


@pytest.mark.parametrize("value", ["true", "false", "obsolete"])
def test_obsolete_push_setting_is_ignored_at_startup(monkeypatch, tmp_path, value):
    monkeypatch.setattr("audit.config.load_dotenv", lambda **_: False)
    monkeypatch.setenv("RUN_MODE", "live")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LIVE_INGESTION_ENABLED", value)
    monkeypatch.setenv("API_AUTH_TOKEN", "")
    parsed = Settings.from_env()
    assert parsed.mode == "live" and parsed.api_auth_token == ""


def test_settings_read_environment_when_constructed(monkeypatch):
    monkeypatch.setenv("MODEL_NAME", "test-model")
    monkeypatch.setenv("API_AUTH_TOKEN", "")
    assert Settings.from_env().model_name == "test-model"


def test_allowed_defaults_and_empty_environment(monkeypatch):
    from audit.config import DEFAULT_ALLOWED_HOSTS, DEFAULT_ALLOWED_ORIGINS

    monkeypatch.setenv("ALLOWED_HOSTS", " ,  , ")
    monkeypatch.setenv("ALLOWED_ORIGINS", "")
    defaults = Settings()
    parsed = Settings.from_env()
    assert defaults.allowed_hosts == parsed.allowed_hosts == DEFAULT_ALLOWED_HOSTS
    assert defaults.allowed_origins == parsed.allowed_origins == DEFAULT_ALLOWED_ORIGINS
    assert defaults.allowed_hosts == {"localhost", "127.0.0.1", "testserver"}
    assert defaults.allowed_origins == {
        "http://localhost:2200", "http://127.0.0.1:2200",
        "http://localhost:2201", "http://127.0.0.1:2201",
    }


def test_allowed_environment_is_additive_trimmed_and_normalized(monkeypatch):
    monkeypatch.setenv("API_AUTH_TOKEN", "a" * 32)
    monkeypatch.setenv("ALLOWED_HOSTS", " Audit.Example.Test, , 192.0.2.1, ::1 ")
    monkeypatch.setenv("ALLOWED_ORIGINS", " HTTPS://Audit.Example.Test:8443, ,http://[::1]:2201 ")
    parsed = Settings.from_env()
    assert parsed.allowed_hosts == Settings().allowed_hosts | {
        "audit.example.test", "192.0.2.1", "::1",
    }
    assert parsed.allowed_origins == Settings().allowed_origins | {
        "https://audit.example.test:8443", "http://[::1]:2201",
    }
    assert isinstance(parsed.allowed_hosts, frozenset)
    assert isinstance(parsed.allowed_origins, frozenset)


@pytest.mark.parametrize("host", [
    "", "*", "*.example.test", "https://audit.example.test", "audit.example.test:2200",
    "audit.example.test/path", "user@audit.example.test", "audit example.test",
    " audit.example.test", "audit.example.test ", "audit..example.test", ".example.test",
    "audit.example.test.", "-audit.example.test", "audit_.example.test", "[::1]",
    "audit.example.test?query", "audit.example.test#fragment", "fe80::1%eth0",
    "a" * 64 + ".example.test", "a." * 127 + "test", "audit\n.example.test",
])
def test_invalid_allowed_hosts_are_rejected_without_echoing_input(host):
    with pytest.raises(ValueError) as exc:
        Settings(allowed_hosts=(host,), api_auth_token="a" * 32)
    assert str(exc.value) == "Invalid ALLOWED_HOSTS entry"


@pytest.mark.parametrize("origin", [
    "", "*", "null", "audit.example.test", "ftp://audit.example.test",
    "https://*.example.test", "https://audit.example.test/", "https://audit.example.test/path",
    "https://audit.example.test?query", "https://audit.example.test?",
    "https://audit.example.test#fragment", "https://audit.example.test#",
    "https://user@audit.example.test", "https://user:password@audit.example.test",
    "https://audit example.test", " https://audit.example.test", "https://audit.example.test\n",
    "https://audit..example.test", "https://audit.example.test:",
    "https://audit.example.test:bad", "https://audit.example.test:65536",
    "https://audit.example.test:99999999999", "https://[127.0.0.1]", "https://[::1%eth0]",
    "https://::1", "https://audit.example.test\\path",
])
def test_invalid_allowed_origins_are_rejected_without_echoing_input(origin):
    with pytest.raises(ValueError) as exc:
        Settings(allowed_origins=(origin,))
    assert str(exc.value) == "Invalid ALLOWED_ORIGINS entry"


@pytest.mark.parametrize("host", ["audit.example.test", "192.0.2.1", "0.0.0.0", "::"])
def test_non_loopback_allowed_hosts_require_token(host):
    with pytest.raises(ValueError, match="^Non-loopback ALLOWED_HOSTS require API_AUTH_TOKEN$"):
        Settings(allowed_hosts=(host,))
    assert host in Settings(allowed_hosts=(host,), api_auth_token="a" * 32).allowed_hosts


def test_loopback_ip_literals_do_not_require_token():
    assert {"127.0.0.2", "::1"} <= Settings(allowed_hosts=("127.0.0.2", "::1")).allowed_hosts


def test_invalid_environment_entries_fail_closed(monkeypatch):
    monkeypatch.setenv("API_AUTH_TOKEN", "")
    monkeypatch.setenv("ALLOWED_HOSTS", "audit.example.test")
    with pytest.raises(ValueError, match="Non-loopback ALLOWED_HOSTS require API_AUTH_TOKEN"):
        Settings.from_env()
    monkeypatch.setenv("API_AUTH_TOKEN", "a" * 32)
    monkeypatch.setenv("ALLOWED_HOSTS", "audit.example.test, *")
    with pytest.raises(ValueError, match="Invalid ALLOWED_HOSTS entry"):
        Settings.from_env()
    monkeypatch.setenv("ALLOWED_HOSTS", "audit.example.test")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://audit.example.test/")
    with pytest.raises(ValueError, match="Invalid ALLOWED_ORIGINS entry"):
        Settings.from_env()


@pytest.mark.parametrize("host", ["192.0.2.001", "0xc0.0.2.1", "192.1", "3221225985", "audit.0x1"])
def test_noncanonical_ipv4_hosts_are_rejected(host):
    with pytest.raises(ValueError, match="Invalid ALLOWED_HOSTS entry"):
        Settings(allowed_hosts=(host,), api_auth_token="x" * 32)
    with pytest.raises(ValueError, match="Invalid ALLOWED_ORIGINS entry"):
        Settings(allowed_origins=(f"https://{host}",))


INTEGRATION_ENV = {
    "MODEL_ENABLED": "true", "SEARCH_PROVIDER": "searxng", "ARCHIVE_BASE_URL": "https://archive.example.invalid",
    "ARCHIVE_WALLET_PATH": "/fabricated/wallet-path", "ARCHIVE_WALLET_JSON": "{}",
    "API_AUTH_TOKEN": "a" * 32,
}


def test_run_mode_defaults_to_live_with_the_live_database(monkeypatch, tmp_path):
    monkeypatch.delenv("RUN_MODE", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    parsed = Settings.from_env()
    assert parsed.mode == "live"
    assert parsed.database_url == f"sqlite:///{tmp_path.resolve() / 'audit.sqlite3'}"


def test_test_mode_ignores_every_integration_and_uses_its_own_database(monkeypatch, tmp_path):
    for name, value in INTEGRATION_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("RUN_MODE", " Test ")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'audit.sqlite3'}")
    parsed = Settings.from_env()
    assert parsed.mode == "test"
    assert parsed.database_url == f"sqlite:///{tmp_path.resolve() / 'demo' / 'audit-test.sqlite3'}"
    assert not parsed.model_enabled and parsed.search_provider == "disabled"
    assert not (parsed.archive_base_url or parsed.archive_wallet_path or parsed.archive_wallet_json)
    assert parsed.api_auth_token == "a" * 32


@pytest.mark.parametrize("override", [
    {"model_enabled": True}, {"search_provider": "searxng"}, {"archive_base_url": "https://archive.example.invalid"},
    {"archive_wallet_path": "/fabricated"}, {"archive_wallet_json": "{}"},
])
def test_test_mode_fails_closed_on_any_integration(override):
    with pytest.raises(ValueError, match="^Test mode runs without model, search or archive$"):
        Settings(mode="test", **override)


def test_modes_never_share_a_database(tmp_path):
    with pytest.raises(ValueError, match="own test database"):
        Settings(mode="test", database_url=f"sqlite:///{tmp_path / 'audit.sqlite3'}")
    with pytest.raises(ValueError, match="own test database"):
        Settings(database_url=f"sqlite:///{tmp_path / 'audit-test.sqlite3'}")
    with pytest.raises(ValueError, match="RUN_MODE"):
        Settings(mode="demo")


@pytest.mark.parametrize("mode,database", [
    ("test", "sqlite:///{tmp}/audit.sqlite3?unused=-test.sqlite3"),
    ("test", "sqlite://"),
    ("test", "sqlite:///:memory:"),
    ("live", "sqlite:///{tmp}/demo/audit-test.sqlite3?timeout=10"),
    ("live", "sqlite:///{tmp}/live-link.sqlite3"),
    ("live", "sqlite:///{tmp}/demo/AUDIT-TEST.SQLITE3"),
    ("live", "sqlite:///{tmp}/data/demo/audit.sqlite3"),
    ("live", "sqlite:///{tmp}/Data/Demo/audit.sqlite3"),
])
def test_database_isolation_uses_the_real_path_not_the_url_text(tmp_path, mode, database):
    # Live mode refuses every database the demo seeder may write (shared rule).
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "audit-test.sqlite3").touch()
    (tmp_path / "live-link.sqlite3").symlink_to(tmp_path / "demo" / "audit-test.sqlite3")
    with pytest.raises(ValueError, match="own test database"):
        Settings(mode=mode, database_url=database.format(tmp=tmp_path))


@pytest.mark.parametrize("database", [
    "sqlite:///file:{tmp}/audit-test.sqlite3?mode=ro&uri=true",
    "sqlite:///{tmp}/audit-test.sqlite3?uri=true",
])
def test_sqlite_uri_filenames_are_refused(tmp_path, database):
    with pytest.raises(ValueError, match="URI"):
        Settings(mode="test", database_url=database.format(tmp=tmp_path))


def test_live_mode_and_the_seeder_share_one_test_database_rule(tmp_path):
    from pathlib import Path
    from audit.config import is_test_database_path
    from audit import demo

    for path in ["data/demo/audit.sqlite3", "x/Data/DEMO/any.db", "audit-test.sqlite3", "x/AUDIT-TEST.SQLITE3"]:
        assert is_test_database_path(Path(path)) and demo.is_test_database(Path(path))
    for path in ["data/audit.sqlite3", "demo/audit.sqlite3", "data/demo", "x/data/demo-old/audit.sqlite3"]:
        assert not is_test_database_path(Path(path)) and not demo.is_test_database(Path(path))
    assert Settings(database_url=f"sqlite:///{tmp_path / 'data' / 'audit.sqlite3'}").mode == "live"


def test_the_checked_database_is_the_one_opened(tmp_path):
    # link -> data/demo/nested; link/../records.sqlite3 resolves under data/demo/, but a lexical
    # normalizer would open live/records.sqlite3. Settings keeps the resolved, checked path.
    nested = tmp_path / "data" / "demo" / "nested"
    nested.mkdir(parents=True)
    (tmp_path / "live").mkdir()
    (tmp_path / "live" / "link").symlink_to(nested)
    tricky = f"sqlite:///{tmp_path / 'live' / 'link'}/../records.sqlite3"
    settings = Settings(mode="test", database_url=tricky)
    assert settings.database_url == f"sqlite:///{(tmp_path / 'data' / 'demo' / 'records.sqlite3').resolve()}"
    with pytest.raises(ValueError, match="own test database"):
        Settings(database_url=tricky)
    assert Settings(database_url=f"sqlite:///{tmp_path / 'live' / 'audit.sqlite3'}?timeout=10").database_url == (
        f"sqlite:///{(tmp_path / 'live' / 'audit.sqlite3').resolve()}")


def test_live_mode_refuses_a_renamed_or_copied_demo_database(tmp_path):
    import shutil
    import sqlite3

    demo_file = tmp_path / "data" / "demo" / "audit-test.sqlite3"
    demo_file.parent.mkdir(parents=True)
    with sqlite3.connect(demo_file) as connection:
        connection.execute("CREATE TABLE workflows (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO workflows VALUES ('wf_demo_fabricated')")
    copy = tmp_path / "elsewhere" / "copy.sqlite3"
    copy.parent.mkdir()
    shutil.copyfile(demo_file, copy)
    before = copy.read_bytes()
    with pytest.raises(ValueError, match="own test database"):
        Settings(database_url=f"sqlite:///{copy}")
    # Fail closed in test mode too: only a test database path qualifies, never a marker alone.
    with pytest.raises(ValueError, match="own test database"):
        Settings(mode="test", database_url=f"sqlite:///{copy}")
    assert copy.read_bytes() == before and sorted(p.name for p in copy.parent.iterdir()) == ["copy.sqlite3"]

    live = tmp_path / "elsewhere" / "live.sqlite3"
    with sqlite3.connect(live) as connection:
        connection.execute("CREATE TABLE workflows (id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO workflows VALUES ('wf_real_fabricated')")
    assert Settings(database_url=f"sqlite:///{live}").mode == "live"
    not_sqlite = tmp_path / "elsewhere" / "notes.sqlite3"
    not_sqlite.write_text("not a database")
    assert Settings(database_url=f"sqlite:///{not_sqlite}").mode == "live"


@pytest.mark.parametrize("provider,thinking,response", [
    ("lmstudio", "lmstudio", "json_schema"), ("openai_compatible", "none", "auto")])
def test_provider_defaults_and_env_overrides(monkeypatch, provider, thinking, response):
    # A developer .env found by walking up from the checkout must not refill the unset vars.
    monkeypatch.setattr("audit.config.load_dotenv", lambda **_: False)
    monkeypatch.setenv("MODEL_PROVIDER", provider)
    monkeypatch.delenv("MODEL_THINKING_CONTROL", raising=False)
    monkeypatch.delenv("MODEL_RESPONSE_FORMAT", raising=False)
    monkeypatch.setenv("MODEL_NAME_RELEVANCE", "fabricated-screen")
    config = Settings.from_env()
    assert config.thinking_control == thinking
    assert config.response_format == response
    assert config.model_overrides()["relevance"] == "fabricated-screen"
    assert config.engine_config()["model_provider"] == provider
    assert config.engine_config()["model_name_relevance"] == "fabricated-screen"
    assert Settings(model_provider=provider).thinking_control == thinking


@pytest.mark.parametrize("kwargs", [
    {"model_provider": "unknown"}, {"response_format": "unknown"},
    {"thinking_control": "unknown"}, {"thinking_control": ""}, {"response_format": ""},
    {"model_provider": "openai_compatible", "thinking_control": "lmstudio"}])
def test_provider_invalid_combinations(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)


def test_live_mode_sees_demo_data_still_in_an_uncheckpointed_wal(tmp_path):
    import shutil
    import sqlite3

    source = tmp_path / "data" / "demo" / "audit-test.sqlite3"
    source.parent.mkdir(parents=True)
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE workflows (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.execute("INSERT INTO workflows VALUES ('wf_demo_fabricated')")
    connection.commit()
    copy = tmp_path / "elsewhere" / "copy.sqlite3"
    copy.parent.mkdir()
    shutil.copyfile(source, copy)
    shutil.copyfile(f"{source}-wal", f"{copy}-wal")
    connection.close()
    from audit.config import has_demo_marker
    assert not has_demo_marker(copy)  # the main file alone has no demo row yet
    with pytest.raises(ValueError, match="own test database"):
        Settings(database_url=f"sqlite:///{copy}")


@pytest.mark.parametrize("mode", ["live", "test"])
@pytest.mark.parametrize("address", ["0.0.0.0", "::", "[::]", "192.0.2.10", "10.0.0.5", "[2001:db8::1]"])
def test_non_loopback_bind_address_requires_token(monkeypatch, mode, address):
    monkeypatch.setattr("audit.config.load_dotenv", lambda **_: False)
    monkeypatch.setenv("RUN_MODE", mode)
    monkeypatch.setenv("KARAKUTU_BIND_ADDRESS", address)
    monkeypatch.setenv("API_AUTH_TOKEN", "")
    for name in ("MODEL_ENABLED", "SEARCH_PROVIDER", "ARCHIVE_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="^Non-loopback KARAKUTU_BIND_ADDRESS requires API_AUTH_TOKEN$"):
        Settings.from_env()
    monkeypatch.setenv("API_AUTH_TOKEN", "a" * 32)
    assert Settings.from_env().bind_address == address


@pytest.mark.parametrize("address", ["", "127.0.0.1", "127.0.0.2", "::1", "[::1]"])
def test_loopback_or_unset_bind_address_needs_no_token(address):
    assert Settings(bind_address=address).bind_address == address


@pytest.mark.parametrize("address", ["localhost", "0.0.0.0:2200", "*", "audit.example.test", "[::1", "[127.0.0.1]x", "[]"])
def test_invalid_bind_address_is_rejected_without_echoing_input(address):
    with pytest.raises(ValueError) as exc:
        Settings(bind_address=address, api_auth_token="a" * 32)
    assert str(exc.value) == "Invalid KARAKUTU_BIND_ADDRESS"


@pytest.mark.parametrize("mode", ["live", "test"])
def test_custom_web_port_adds_its_loopback_origins(monkeypatch, mode):
    monkeypatch.setattr("audit.config.load_dotenv", lambda **_: False)
    monkeypatch.setenv("RUN_MODE", mode)
    monkeypatch.setenv("KARAKUTU_WEB_PORT", "23101")
    for name in ("MODEL_ENABLED", "SEARCH_PROVIDER", "ARCHIVE_BASE_URL",
                 "API_AUTH_TOKEN", "ALLOWED_ORIGINS", "ALLOWED_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    origins = Settings.from_env().allowed_origins
    assert {"http://localhost:23101", "http://127.0.0.1:23101"} <= origins
    assert not any(origin.endswith(":23101") and "localhost" not in origin and "127.0.0.1" not in origin
                   for origin in origins)


def test_default_web_port_keeps_the_default_origins():
    assert Settings(web_port=2201).allowed_origins == Settings().allowed_origins


@pytest.mark.parametrize("value", ["0", "65536", "-1", "port", "2201x"])
def test_invalid_web_port_is_rejected(monkeypatch, value):
    monkeypatch.setattr("audit.config.load_dotenv", lambda **_: False)
    monkeypatch.setenv("KARAKUTU_WEB_PORT", value)
    with pytest.raises(ValueError, match="^Invalid KARAKUTU_WEB_PORT$"):
        Settings.from_env()


def test_web_port_80_adds_origins_the_way_browsers_send_them():
    origins = Settings(web_port=80).allowed_origins
    assert {"http://localhost", "http://127.0.0.1"} <= origins
    assert "http://localhost:80" not in origins

from __future__ import annotations

from pathlib import Path

import pytest

from termwatch.api import origin_allowed
from termwatch.config import ConfigError, is_loopback_host, load_settings, write_default_config
from termwatch.terminal_text import last_visible_line, strip_ansi


def test_defaults_bind_to_loopback(home: Path) -> None:
    settings = load_settings()
    assert settings.host == "127.0.0.1"
    assert settings.port == 8765
    assert settings.config_dir == home


def test_precedence_file_env_override(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home.mkdir(parents=True)
    (home / "config.toml").write_text('port = 9000\nmax_sessions = 10\nhost = "100.64.0.1"\n')
    monkeypatch.setenv("TERMWATCH_PORT", "9100")
    settings = load_settings({"host": None})
    assert settings.port == 9100
    assert settings.max_sessions == 10
    assert settings.host == "100.64.0.1"
    assert load_settings({"port": 9200}).port == 9200


def test_invalid_config_is_reported(home: Path) -> None:
    home.mkdir(parents=True)
    (home / "config.toml").write_text("bogus = 1\n")
    with pytest.raises(ConfigError, match="unknown keys"):
        load_settings()
    (home / "config.toml").write_text('cors_origins = ["*"]\n')
    with pytest.raises(ConfigError, match="wildcard"):
        load_settings()


def test_default_template_parses(home: Path) -> None:
    assert write_default_config(home / "config.toml")
    assert not write_default_config(home / "config.toml")
    load_settings()


def test_loopback_detection() -> None:
    assert is_loopback_host("127.0.0.1") and is_loopback_host("::1") and is_loopback_host("localhost")
    assert not is_loopback_host("0.0.0.0") and not is_loopback_host("100.101.102.103")


def test_origin_policy() -> None:
    assert origin_allowed(None, "127.0.0.1:8765", ())
    assert origin_allowed("http://127.0.0.1:8765", "127.0.0.1:8765", ())
    assert not origin_allowed("http://evil.example", "127.0.0.1:8765", ())
    assert origin_allowed("http://localhost:5173", "127.0.0.1:8765", ("http://localhost:5173",))


def test_text_helpers() -> None:
    assert strip_ansi("\x1b[1;31mred\x1b[0m") == "red"
    assert last_visible_line("a\r\nb\r\n") == "b"
    assert last_visible_line(" 10%|##  |\r 90%|######|\r") == "90%|######|"
    assert last_visible_line("\n \n") is None
    assert len(last_visible_line("x" * 1000) or "") == 300

"""`.env` and TOML config files written by Windows editors and shells.

Notepad and PowerShell write a UTF-8 byte-order mark; Windows PowerShell 5.1's `>` writes
UTF-16. These are byte-level facts, so the tests run on every platform.
"""

from __future__ import annotations

import os

import pytest

from jarvis.config.env_file import EnvFileError, load_env_file
from jarvis.config.manager import load_config

KEY = "JARVIS_TEST_ENCODING_KEY"
SECOND = "JARVIS_TEST_ENCODING_SECOND"
BODY = f"{KEY}=first\n{SECOND}=second\n"
BOM = b"\xef\xbb\xbf"


@pytest.fixture
def isolated_environ(monkeypatch):
    """Let load_dotenv write somewhere that is discarded, and enable it (conftest disables it)."""
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.setattr(os, "environ", dict(os.environ))
    return os.environ


# --- .env ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(BODY.encode("utf-8"), id="utf8"),
        pytest.param(BOM + BODY.encode("utf-8"), id="utf8-bom"),
        pytest.param(BODY.replace("\n", "\r\n").encode("utf-8"), id="utf8-crlf"),
        pytest.param(BOM + BODY.replace("\n", "\r\n").encode("utf-8"), id="utf8-bom-crlf"),
    ],
)
def test_env_file_loads_every_key_including_the_first(tmp_path, isolated_environ, raw):
    env_file = tmp_path / ".env"
    env_file.write_bytes(raw)
    assert load_env_file(env_file) is True
    # With a BOM, python-dotenv alone would store the first key as "﻿<name>".
    assert isolated_environ[KEY] == "first"
    assert isolated_environ[SECOND] == "second"
    assert not [name for name in isolated_environ if name.startswith("﻿")]


def test_utf16_env_file_is_a_clear_error_not_a_traceback(tmp_path, isolated_environ):
    env_file = tmp_path / ".env"
    env_file.write_bytes(BODY.encode("utf-16"))  # what PowerShell 5.1 '>' produces
    with pytest.raises(EnvFileError, match="not UTF-8") as raised:
        load_env_file(env_file)
    assert "Re-save it as UTF-8" in str(raised.value)
    assert "first" not in str(raised.value), "file content must never appear in the error"


def test_missing_env_file_is_not_an_error(tmp_path, isolated_environ):
    assert load_env_file(tmp_path / "does-not-exist.env") is False


def test_existing_environment_wins_over_the_env_file(tmp_path, isolated_environ):
    isolated_environ[KEY] = "from-environment"
    env_file = tmp_path / ".env"
    env_file.write_bytes(BOM + BODY.encode("utf-8"))
    load_env_file(env_file)
    assert isolated_environ[KEY] == "from-environment"
    assert isolated_environ[SECOND] == "second"


# --- TOML config --------------------------------------------------------------------------

TOML = '[server]\nport = 8123\n'


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(TOML.encode("utf-8"), id="utf8"),
        pytest.param(BOM + TOML.encode("utf-8"), id="utf8-bom"),
        pytest.param(TOML.replace("\n", "\r\n").encode("utf-8"), id="utf8-crlf"),
        pytest.param(BOM + TOML.replace("\n", "\r\n").encode("utf-8"), id="utf8-bom-crlf"),
    ],
)
def test_config_file_with_bom_or_crlf_loads(tmp_path, monkeypatch, raw):
    monkeypatch.delenv("JARVIS_SERVER_PORT", raising=False)
    config_file = tmp_path / "jarvis.toml"
    config_file.write_bytes(raw)
    assert load_config(config_file).server.port == 8123


def test_utf16_config_file_is_a_clear_error(tmp_path):
    config_file = tmp_path / "jarvis.toml"
    config_file.write_bytes(TOML.encode("utf-16"))
    with pytest.raises(ValueError, match="not UTF-8 text"):
        load_config(config_file)


def test_invalid_toml_is_still_reported_as_invalid_toml(tmp_path):
    import tomllib

    config_file = tmp_path / "jarvis.toml"
    config_file.write_bytes(BOM + b"[server\nport = 1\n")
    with pytest.raises(tomllib.TOMLDecodeError):
        load_config(config_file)

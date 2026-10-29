"""F-12: the database location does not depend on the working directory.

conftest.py points the per-user data directory at a temporary directory; the subprocess tests
use the real platformdirs lookup but only print the resolved path (nothing is created).
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import jarvis
from jarvis.api.http import create_app
from jarvis.config import paths
from jarvis.config.manager import load_config
from jarvis.config.paths import DatabaseLocationError
from jarvis.config.secrets import SecretManager
from jarvis.storage.connection import resolve_database_path

TOKEN = "DUMMY-db-location-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def _secrets() -> SecretManager:
    return SecretManager({"JARVIS_API_TOKEN": TOKEN}, use_keyring=False)


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    a, b = tmp_path / "launch-a", tmp_path / "launch-b" / "nested"
    a.mkdir()
    b.mkdir(parents=True)
    return a, b


def _start(**kwargs: object) -> TestClient:
    return TestClient(create_app(config=load_config(), secret_manager=_secrets(), **kwargs))


# --- Default location ----------------------------------------------------------------------


def test_default_is_the_per_user_data_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_user_data_dir(*args: object, **kwargs: object) -> str:
        calls.append((args, kwargs))
        return os.path.join(os.sep, "users", "me", "data", "Jarvis")

    monkeypatch.setattr(paths, "user_data_dir", fake_user_data_dir)

    expected = Path(os.sep, "users", "me", "data", "Jarvis", "memory.sqlite")
    assert paths.default_database_path() == expected
    # appauthor=False: no "<author>\Jarvis" nesting on Windows; roaming=True: %APPDATA%.
    assert calls == [(("Jarvis",), {"appauthor": False, "roaming": True})]


def test_windows_default_maps_to_appdata_jarvis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulated: platformdirs' Windows implementation with a fake known-folder lookup."""
    import platformdirs.windows as windows

    appdata = r"C:\Users\me\AppData\Roaming"
    monkeypatch.setattr(windows, "get_win_folder", lambda csidl: {"CSIDL_APPDATA": appdata}[csidl])

    data_dir = windows.Windows(appname="Jarvis", appauthor=False, roaming=True).user_data_dir
    assert data_dir.replace("\\", "/") == "C:/Users/me/AppData/Roaming/Jarvis"


def test_default_resolves_identically_from_any_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved = []
    for cwd in _dirs(tmp_path):
        monkeypatch.chdir(cwd)
        resolved.append(resolve_database_path(load_config()))

    assert resolved[0] == resolved[1] == paths.default_database_path()
    assert resolved[0].is_absolute()
    assert not any(str(p).startswith(str(tmp_path / "launch")) for p in resolved)


def test_daemon_uses_one_database_from_different_working_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _dirs(tmp_path)

    monkeypatch.chdir(a)
    with _start() as client:
        created = client.post("/v1/projects", json={"name": "persisted"}, headers=HEADERS)
        assert created.status_code == 200
        storage = client.app.state.storage_status  # what /v1/status and `jarvis status` show

    monkeypatch.chdir(b)
    with _start() as client:
        names = [p["name"] for p in client.get("/v1/projects", headers=HEADERS).json()]

    assert names == ["persisted"]
    assert storage["database_path"] == str(paths.default_database_path())
    assert paths.default_database_path().is_file()
    for cwd in (a, b):
        assert list(cwd.iterdir()) == []  # no stray database next to the shell


def test_cli_reports_the_same_database_from_different_working_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from jarvis.app.terminal import app

    reported = []
    for cwd in _dirs(tmp_path):
        monkeypatch.chdir(cwd)
        result = CliRunner().invoke(app, ["config", "show"])
        assert result.exit_code == 0, result.output
        reported.append(json.loads(result.output)["memory"]["database_path"])

    assert reported[0] == reported[1] == str(paths.default_database_path())


def test_separate_processes_resolve_the_same_absolute_path(tmp_path: Path) -> None:
    """Real platformdirs lookup in fresh interpreters started from two directories."""
    home = tmp_path / "home"
    home.mkdir()
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("JARVIS_") and k not in ("OPENROUTER_API_KEY", "PYTHONPATH")
    }
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        XDG_DATA_HOME=str(home / "xdg"),
        # Windows: platformdirs >= 4.10 honours these instead of the real known folders.
        WIN_PD_OVERRIDE_APPDATA=str(home / "AppData" / "Roaming"),
        WIN_PD_OVERRIDE_LOCAL_APPDATA=str(home / "AppData" / "Local"),
        PYTHONPATH=str(Path(jarvis.__file__).resolve().parents[1]),
        PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring",
        PYTHONDONTWRITEBYTECODE="1",
    )
    code = (
        "from jarvis.config.manager import load_config;"
        "from jarvis.storage.connection import resolve_database_path;"
        "print(resolve_database_path(load_config()))"
    )
    a, b = _dirs(tmp_path)
    outputs = []
    for cwd in (a, b):
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True, text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout.strip())

    assert outputs[0] == outputs[1]
    resolved = Path(outputs[0])
    assert resolved.is_absolute() and resolved.name == "memory.sqlite"
    assert resolved.parent.name == "Jarvis"
    if resolved.is_relative_to(home):
        assert not resolved.exists()
    else:
        # Only an older platformdirs on Windows ignores the overrides; the real profile path
        # was then only printed, never opened or created.
        assert sys.platform == "win32"
    for cwd in (a, b):
        assert list(cwd.iterdir()) == []


# --- Explicit configuration ------------------------------------------------------------------


def test_explicit_absolute_paths_are_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _dirs(tmp_path)
    from_env = tmp_path / "env" / "jarvis.sqlite"
    from_file = tmp_path / "file" / "jarvis.sqlite"
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"[memory]\ndatabase_path = '{from_file.as_posix()}'\n", encoding="utf-8"
    )

    for cwd in (a, b):
        monkeypatch.chdir(cwd)
        assert resolve_database_path(load_config(config_file)) == from_file
        monkeypatch.setenv("JARVIS_DATABASE_PATH", str(from_env))
        assert resolve_database_path(load_config(config_file)) == from_env  # env wins
        monkeypatch.delenv("JARVIS_DATABASE_PATH")

    monkeypatch.setenv("JARVIS_DATABASE_PATH", str(from_env))
    with _start() as client:
        storage = client.app.state.storage_status
    assert storage["database_path"] == str(from_env)
    assert from_env.is_file()
    assert not paths.default_database_path().exists()


def test_home_and_environment_variables_are_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_TEST_DATA", str(tmp_path / "data"))

    monkeypatch.setenv("JARVIS_DATABASE_PATH", "~/jarvis/memory.sqlite")
    assert resolve_database_path(load_config()) == tmp_path / "home" / "jarvis" / "memory.sqlite"

    variable = "%JARVIS_TEST_DATA%" if sys.platform == "win32" else "${JARVIS_TEST_DATA}"
    monkeypatch.setenv("JARVIS_DATABASE_PATH", f"{variable}/memory.sqlite")
    assert resolve_database_path(load_config()) == tmp_path / "data" / "memory.sqlite"


def test_relative_paths_keep_their_meaning_and_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    a, _ = _dirs(tmp_path)
    monkeypatch.chdir(a)
    monkeypatch.setenv("JARVIS_DATABASE_PATH", "data/jarvis.sqlite")

    assert resolve_database_path(load_config()) == a / "data" / "jarvis.sqlite"
    with caplog.at_level(logging.WARNING, logger="jarvis.api.http"), _start():
        pass
    assert "is relative, so it depends on the working directory" in caplog.text
    assert (a / "data" / "jarvis.sqlite").is_file()


# --- Previous default location ---------------------------------------------------------------


def _legacy_database(base: Path) -> tuple[Path, str]:
    legacy = base / ".jarvis" / "dev" / "memory.sqlite"
    with TestClient(create_app(
        config=load_config(), secret_manager=_secrets(), database_path=legacy
    )) as client:
        assert client.post("/v1/projects", json={"name": "old"}, headers=HEADERS).status_code == 200
    return legacy, hashlib.sha256(legacy.read_bytes()).hexdigest()


@pytest.mark.parametrize("location", ["working-directory", "source-checkout"])
def test_existing_legacy_database_blocks_a_new_empty_default(
    location: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = _dirs(tmp_path)
    base = a if location == "working-directory" else paths.source_root()
    legacy, digest = _legacy_database(base)
    monkeypatch.chdir(a)

    with pytest.raises(DatabaseLocationError) as excinfo:
        with _start():
            pass

    message = str(excinfo.value)
    assert str(legacy) in message
    assert "JARVIS_DATABASE_PATH" in message
    assert str(paths.default_database_path().parent) in message
    assert not paths.default_database_path().exists()  # no second, empty database
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == digest  # untouched


def test_legacy_database_stays_usable_through_explicit_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _dirs(tmp_path)
    legacy, _ = _legacy_database(a)
    monkeypatch.chdir(b)
    monkeypatch.setenv("JARVIS_DATABASE_PATH", str(legacy))

    with _start() as client:
        names = [p["name"] for p in client.get("/v1/projects", headers=HEADERS).json()]

    assert names == ["old"]
    assert not paths.default_database_path().exists()


def test_existing_default_wins_over_legacy_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    a, _ = _dirs(tmp_path)
    legacy, digest = _legacy_database(a)
    monkeypatch.chdir(tmp_path)
    with _start():  # no legacy database visible from here: creates the default
        pass
    monkeypatch.chdir(a)

    with caplog.at_level(logging.WARNING, logger="jarvis.config.paths"), _start() as client:
        names = [p["name"] for p in client.get("/v1/projects", headers=HEADERS).json()]

    assert names == []  # the default database, not the legacy one
    assert str(legacy) in caplog.text
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == digest


def test_explicitly_configured_default_path_is_not_blocked_by_a_legacy_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _ = _dirs(tmp_path)
    _legacy_database(a)
    monkeypatch.chdir(a)
    monkeypatch.setenv("JARVIS_DATABASE_PATH", str(paths.default_database_path()))

    with _start() as client:  # the user chose this file: no DatabaseLocationError
        assert client.get("/v1/projects", headers=HEADERS).json() == []
    assert paths.default_database_path().is_file()


def test_other_environment_overrides_keep_the_path_a_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_SERVER_PORT", "9999")
    config = load_config()
    assert config.server.port == 9999
    assert "database_path" not in config.memory.model_fields_set  # still subject to the check
    monkeypatch.setenv("JARVIS_DATABASE_PATH", "/explicit.sqlite")
    assert "database_path" in load_config().memory.model_fields_set


def test_environment_variable_expansion_per_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_TEST_VAR", "expanded")
    # Windows: only %VAR%; "$" is an ordinary path character there.
    assert paths.expand_env_vars(r"C:\%JARVIS_TEST_VAR%\db", windows=True) == r"C:\expanded\db"
    for literal in (r"D:\$JARVIS_TEST_VAR\db", r"C:\%JARVIS_UNSET_VAR%\db"):
        assert paths.expand_env_vars(literal, windows=True) == literal
    # Elsewhere: $VAR and ${VAR}.
    assert paths.expand_env_vars("/srv/${JARVIS_TEST_VAR}/db", windows=False) == "/srv/expanded/db"
    assert paths.expand_env_vars("/srv/$JARVIS_TEST_VAR/db", windows=False) == "/srv/expanded/db"

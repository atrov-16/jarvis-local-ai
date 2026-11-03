"""Cross-platform hardening: single daemon per database, line endings, workspace and path
policy on case-insensitive and Windows filesystems, CLI addressing, command capability policy.

Behaviour that depends on the operating system runs against the real one; tests are skipped
only on the platform that lacks the mechanism.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.app import daemon as daemon_module
from jarvis.app.instance_lock import DaemonAlreadyRunningError, InstanceLock
from jarvis.config.manager import load_config
from jarvis.config.secrets import SecretManager
from jarvis.tools.filesystem import PatchFileTool, PatchHunkInput, WriteFileTool
from jarvis.tools.generic_command import unsupported_on_this_platform
from jarvis.tools.git import GitTool
from jarvis.tools.path_policy import authorize_path
from jarvis.workspaces.registry import dangerous_root_reason

WINDOWS = sys.platform == "win32"
DARWIN = sys.platform == "darwin"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows filesystem semantics")
darwin_only = pytest.mark.skipif(not DARWIN, reason="case-insensitive macOS volume")
posix_only = pytest.mark.skipif(WINDOWS, reason="POSIX behaviour")


# --- One daemon per database -------------------------------------------------------------


def test_second_lock_on_the_same_database_is_refused(tmp_path):
    database = tmp_path / "data" / "memory.sqlite"
    with InstanceLock(database):
        with pytest.raises(DaemonAlreadyRunningError, match="already running"):
            InstanceLock(database).acquire()
    again = InstanceLock(database)
    again.acquire()  # released on exit: free again
    again.release()


def test_lock_is_held_across_processes_and_released_when_the_holder_dies(tmp_path):
    database = tmp_path / "memory.sqlite"
    holder = subprocess.Popen(
        [
            sys.executable, "-c",
            "import time; from pathlib import Path; "
            "from jarvis.app.instance_lock import InstanceLock; "
            f"lock = InstanceLock(Path({str(database)!r})); lock.acquire(); print('locked', flush=True); "
            "time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(DaemonAlreadyRunningError):
            InstanceLock(database).acquire()
    finally:
        holder.kill()
        holder.wait()
    again = InstanceLock(database)
    again.acquire()  # the OS released the dead holder's lock: no stale lock
    again.release()


def test_daemon_refuses_to_start_when_another_holds_the_database(tmp_path, monkeypatch):
    config_file = tmp_path / "jarvis.toml"
    database = tmp_path / "db" / "memory.sqlite"
    config_file.write_text(f'[memory]\ndatabase_path = "{database.as_posix()}"\n')
    ran = []
    monkeypatch.setattr(daemon_module.uvicorn, "run", lambda *a, **k: ran.append(1))
    with InstanceLock(database):
        with pytest.raises(SystemExit, match="already running"):
            daemon_module.run_daemon(config_file)
    assert not ran, "uvicorn (and so startup recovery) must not run"


# --- Line endings --------------------------------------------------------------------------


async def test_patching_a_crlf_file_keeps_crlf(tmp_path):
    (tmp_path / "f.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    result = await PatchFileTool().execute(
        path="f.txt", hunks=[PatchHunkInput(search="two", replace="TWO")],
        workspaces=[{"path": str(tmp_path)}],
    )
    assert result.success, result.error
    assert (tmp_path / "f.txt").read_bytes() == b"one\r\nTWO\r\nthree\r\n"


async def test_patching_an_lf_file_keeps_lf(tmp_path):
    (tmp_path / "f.txt").write_bytes(b"one\ntwo\nthree\n")
    result = await PatchFileTool().execute(
        path="f.txt", hunks=[PatchHunkInput(search="two\nthree", replace="2\n3")],
        workspaces=[{"path": str(tmp_path)}],
    )
    assert result.success, result.error
    assert (tmp_path / "f.txt").read_bytes() == b"one\n2\n3\n"


async def test_write_file_stores_content_exactly(tmp_path):
    result = await WriteFileTool().execute(
        path="out.txt", content="a\nb\r\nc\n", workspaces=[{"path": str(tmp_path)}]
    )
    assert result.success, result.error
    assert (tmp_path / "out.txt").read_bytes() == b"a\nb\r\nc\n"


# --- Git failures are not reported as a clean repository -------------------------------------


async def test_git_status_outside_a_repository_fails(tmp_path, monkeypatch):
    from jarvis.tasks.command_runner import CommandRunner

    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    (tmp_path / ".git").mkdir()  # looks like a repository, but git cannot use it
    result = await GitTool(CommandRunner()).execute(
        operation="status", workspaces=[{"path": str(tmp_path)}]
    )
    assert not result.success
    assert "Git status failed" in (result.error or "")


# --- Workspace roots and protected paths ---------------------------------------------------


@darwin_only
@pytest.mark.parametrize("root", ["/USERS", "/system/Library", "/Applications", "/USR/local"])
def test_dangerous_roots_are_matched_case_insensitively_on_macos(root):
    assert dangerous_root_reason(Path(root)) is not None


@darwin_only
def test_home_directory_in_other_case_is_still_refused_on_macos():
    assert dangerous_root_reason(Path(str(Path.home().resolve()).upper())) is not None


@windows_only
@pytest.mark.parametrize(
    "root", [r"\\?\C:\Windows\System32", r"\\?\C:\Users", r"c:\WINDOWS", "\\\\?\\C:\\"]
)
def test_windows_extended_prefix_and_case_do_not_bypass_the_denylist(root):
    assert dangerous_root_reason(Path(root)) is not None


@windows_only
@pytest.mark.parametrize("name", [".env::$DATA", ".env.", ".env ", ".git.", ".GIT ", ".env:stream"])
def test_windows_protected_name_aliases_are_denied(tmp_path, name):
    (tmp_path / ".env").write_text("SECRET=1")
    with pytest.raises(PermissionError):
        authorize_path(str(tmp_path / name), [{"path": str(tmp_path)}])


@posix_only
def test_posix_backslash_is_an_ordinary_filename_character(tmp_path):
    authorized = authorize_path("a\\b.txt", [{"path": str(tmp_path)}])
    assert authorized.path.name == "a\\b.txt"


# --- Command capability policy ---------------------------------------------------------------


@pytest.mark.skipif(WINDOWS, reason="policy applies to Windows only")
@pytest.mark.parametrize("binary", ["ls", "grep", "find", "make", "gcc", "g++", "node"])
def test_no_platform_restriction_on_posix(binary):
    assert unsupported_on_this_platform(binary, "/usr/bin") is None


@windows_only
@pytest.mark.parametrize("binary", ["ls", "grep", "find", "make", "gcc", "g++"])
def test_unix_toolchain_binaries_are_not_assumed_on_windows(binary, tmp_path):
    # Nothing installed on this PATH.
    message = unsupported_on_this_platform(binary, str(tmp_path))
    assert message is not None and "optional Unix toolchain" in message


@windows_only
def test_system32_find_is_never_used_as_unix_find():
    system32 = os.path.join(os.environ["SYSTEMROOT"], "System32")
    assert unsupported_on_this_platform("find", system32) is not None


@windows_only
def test_an_installed_toolchain_binary_is_allowed(tmp_path):
    (tmp_path / "grep.exe").write_text("")
    assert unsupported_on_this_platform("grep", str(tmp_path)) is None


# --- CLI addressing, configuration, credentials ---------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [("0.0.0.0", "http://127.0.0.1:8765"), ("::", "http://[::1]:8765"),
     ("127.0.0.1", "http://127.0.0.1:8765"), ("localhost", "http://localhost:8765")],
)
def test_cli_never_connects_to_a_wildcard_address(tmp_path, host, expected):
    from jarvis.app.terminal import _get_api_client

    config = tmp_path / "c.toml"
    config.write_text(f'[server]\nhost = "{host}"\nport = 8765\n[security]\nlocalhost_only = false\n')
    with _get_api_client(config) as client:
        assert str(client.base_url).rstrip("/") == expected


def test_cli_ignores_system_proxies(monkeypatch):
    from jarvis.app.terminal import _get_api_client

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://proxy.invalid:3128")
    with _get_api_client(None) as client:
        assert client.trust_env is False


def test_workspace_add_sends_an_absolute_path(tmp_path, monkeypatch):
    import httpx
    from typer.testing import CliRunner

    from jarvis.app import terminal

    sent = {}

    def fake_post(self, url, **kwargs):
        sent.update(kwargs["json"])
        return httpx.Response(200, json={"path": kwargs["json"]["path"]},
                              request=httpx.Request("POST", "http://x"))

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "proj").mkdir()
    result = CliRunner().invoke(terminal.app, ["workspace", "add", "proj"])
    assert result.exit_code == 0, result.output
    assert Path(sent["path"]) == (tmp_path / "proj").resolve() or Path(sent["path"]) == (
        tmp_path / "proj"
    )
    assert Path(sent["path"]).is_absolute()


def test_published_placeholder_token_is_not_accepted():
    placeholder = SecretManager(env={"JARVIS_API_TOKEN": "change-me-local-token"}, use_keyring=False)
    assert placeholder.get_api_token() is None
    real = SecretManager(env={"JARVIS_API_TOKEN": "a-real-token"}, use_keyring=False)
    assert real.get_api_token() == "a-real-token"


def test_explicit_missing_config_file_is_an_error_not_defaults(tmp_path):
    with pytest.raises(FileNotFoundError, match="Config file not found"):
        load_config(tmp_path / "missing.toml")


def test_config_path_is_expanded(tmp_path, monkeypatch):
    (tmp_path / "jarvis.toml").write_text("[server]\nport = 9123\n")
    monkeypatch.setenv("JARVIS_TEST_DIR", str(tmp_path))
    reference = "%JARVIS_TEST_DIR%" if WINDOWS else "$JARVIS_TEST_DIR"
    assert load_config(reference + os.sep + "jarvis.toml").server.port == 9123


@posix_only
async def test_new_data_directory_and_database_are_private(tmp_path):
    from jarvis.storage.connection import sqlite_connection

    database = tmp_path / "newdir" / "memory.sqlite"
    async with sqlite_connection(database):
        pass
    assert (database.parent.stat().st_mode & 0o777) == 0o700
    assert (database.stat().st_mode & 0o777) == 0o600

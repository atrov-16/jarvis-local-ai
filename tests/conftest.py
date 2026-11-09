"""Isolation from the developer's real credentials and data, for every test.

- The OS credential store (macOS login Keychain, Windows Credential Manager) is replaced by an
  in-memory backend before any test module runs, so nothing reads, writes or deletes real
  credentials, even through `SecretManager()` with its defaults.
- python-dotenv is disabled for the session (jarvis.app.terminal calls load_dotenv() at
  import), so a developer's real .env is never loaded, in this process or in child processes.
- Secret and configuration environment variables are removed for each test and restored
  exactly afterwards.
- The per-user data directory and the repository-root legacy database location resolve to
  temporary directories, so a default-configured app never opens the real database.
- httpx.AsyncClient has no network by default: model providers (e.g. a local Ollama the app
  falls back to) are never contacted for real. Tests that need responses patch the client or
  pass a mock transport.
- TestClient connects from 127.0.0.1 by default, a loopback peer for localhost_only.

It also applies the platform markers (``windows``, ``macos``, ``posix``; see _platforms.py),
so the suite runs to completion on each OS by itself and reports what is left for the other.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

# Before any jarvis.app module is imported (they load .env at import time).
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

from _platforms import MARKERS, host_label, other_platforms, platform_skip_reason  # noqa: E402

from jarvis.config import paths  # noqa: E402

_PLATFORM_SKIPS = pytest.StashKey[int]()


def pytest_configure(config: pytest.Config) -> None:
    for name, description in MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {description}")


def pytest_report_header(config: pytest.Config) -> str:
    return (
        f"platform: {host_label()} ({sys.platform}); tests marked for other platforms "
        "are skipped here, not failed"
    )


@pytest.hookimpl(trylast=True)  # after -m/-k deselection, so only selected tests are counted
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skipped = 0
    for item in items:
        try:
            reason = platform_skip_reason(mark.name for mark in item.iter_markers())
        except ValueError as exc:
            raise pytest.UsageError(f"{item.nodeid}: {exc}") from None
        if reason is not None:
            item.add_marker(pytest.mark.skip(reason=reason))
            skipped += 1
    config.stash[_PLATFORM_SKIPS] = skipped


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, exitstatus: int, config: pytest.Config
) -> None:
    skipped = config.stash.get(_PLATFORM_SKIPS, 0)
    if skipped:
        terminalreporter.write_line(
            f"{skipped} platform-specific test(s) skipped on {host_label()}; "
            f"run `uv run pytest` on {' and '.join(other_platforms())} to cover them."
        )

ISOLATED_ENV_VARS = (
    "JARVIS_API_TOKEN",
    "OPENROUTER_API_KEY",
    "JARVIS_CONFIG_PATH",
    "JARVIS_DATABASE_PATH",
    "JARVIS_SERVER_HOST",
    "JARVIS_SERVER_PORT",
)


class _NoNetworkTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"network access is disabled in tests: {request.url}", request=request
        )


class OfflineAsyncClient(httpx.AsyncClient):
    """httpx.AsyncClient whose default transport refuses every connection."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("transport", _NoNetworkTransport())
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]


class IsolatedTestKeyring(KeyringBackend):
    """In-memory credential store; its priority keeps keyring from ever selecting it itself."""

    priority = 0

    def __init__(self) -> None:
        super().__init__()
        self.passwords: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.passwords.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.passwords[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.passwords[(service, username)]
        except KeyError:
            raise PasswordDeleteError(username) from None


TEST_KEYRING = IsolatedTestKeyring()
# Installed at import, before test modules are collected: the real backend is never initialised.
keyring.set_keyring(TEST_KEYRING)


@pytest.fixture(autouse=True)
def isolated_host_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[IsolatedTestKeyring]:
    # Re-installed per test, so a test that swaps the backend cannot leak it into the next.
    keyring.set_keyring(TEST_KEYRING)
    TEST_KEYRING.passwords.clear()

    for name in ISOLATED_ENV_VARS:
        # setenv first records the original value, so even a value that the test assigns to
        # os.environ directly is removed again on teardown.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)

    data_dir = tmp_path_factory.mktemp("user-data")
    monkeypatch.setattr(paths, "user_data_dir", lambda *args, **kwargs: str(data_dir / "Jarvis"))
    source_root = tmp_path_factory.mktemp("source-root")
    monkeypatch.setattr(paths, "source_root", lambda: Path(source_root))

    monkeypatch.setattr(httpx, "AsyncClient", OfflineAsyncClient)

    from starlette import testclient

    original_init = testclient.TestClient.__init__

    def loopback_init(self, *args, client=("127.0.0.1", 50000), **kwargs):  # type: ignore[no-untyped-def]
        original_init(self, *args, client=client, **kwargs)

    monkeypatch.setattr(testclient.TestClient, "__init__", loopback_init)

    yield TEST_KEYRING
    TEST_KEYRING.passwords.clear()


@pytest.fixture
def unix_toolchain(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Make the optional Unix toolchain (ls, grep, find, make, gcc, g++) look installed.

    Windows has none of these, and the command capability policy refuses them when absent
    (tested in test_xplat_hardening). Tests of the argument and approval policy that run
    them through a mocked runner use this, so they do not depend on whether Git's Unix
    tools happen to be on the PATH of the machine running them. A no-op on macOS/Linux,
    where the real programs exist.
    """
    import sys

    if sys.platform != "win32":
        return
    from jarvis.tools.generic_command import WINDOWS_OPTIONAL_TOOLCHAIN

    stand_ins = tmp_path / "unix-toolchain"
    stand_ins.mkdir()
    for name in WINDOWS_OPTIONAL_TOOLCHAIN:
        (stand_ins / f"{name}.exe").write_bytes(b"")
    monkeypatch.setenv("PATH", f"{stand_ins}{os.pathsep}{os.environ.get('PATH', '')}")

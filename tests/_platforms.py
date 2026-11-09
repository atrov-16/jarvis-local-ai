"""Platform markers: which tests run on which operating system.

Mark a test with ``@pytest.mark.windows``, ``@pytest.mark.macos`` or ``@pytest.mark.posix``
(macOS and Linux). Unmarked tests run everywhere. On every machine, ``uv run pytest`` runs the
shared tests plus that machine's own, and skips the rest with a reason that says where they
run, so a Windows run and a macOS run are each complete on their own and neither needs the
other machine.

A test that could run nowhere (for example marked both ``windows`` and ``posix``) is an error,
not a silent skip: every test must run on at least one platform.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable

MARKERS: dict[str, str] = {
    "windows": "runs only on Windows",
    "macos": "runs only on macOS",
    "posix": "runs only on POSIX systems (macOS and Linux)",
}

_LABELS = {"win32": "Windows", "darwin": "macOS"}


def host_label(platform: str = sys.platform) -> str:
    return _LABELS.get(platform, "Linux" if platform.startswith("linux") else platform)


def _runs_on(marker: str, platform: str) -> bool:
    if marker == "windows":
        return platform == "win32"
    if marker == "macos":
        return platform == "darwin"
    return platform != "win32"  # posix


def platform_skip_reason(markers: Iterable[str], platform: str = sys.platform) -> str | None:
    """Why a test with these markers is skipped on ``platform``, or None if it runs there.

    Raises ValueError when the markers exclude every platform.
    """
    wanted = sorted({m for m in markers if m in MARKERS})
    if not wanted:
        return None
    candidates = ("win32", "darwin", "linux")
    if not any(all(_runs_on(m, p) for m in wanted) for p in candidates):
        raise ValueError(
            f"marked {' + '.join(wanted)}, which no platform satisfies, so it would never run"
        )
    if all(_runs_on(m, platform) for m in wanted):
        return None
    targets = sorted({host_label(p) for p in candidates if all(_runs_on(m, p) for m in wanted)})
    return f"{'/'.join(targets)} only; run it there (this is {host_label(platform)})"


def other_platforms(platform: str = sys.platform) -> list[str]:
    """The platforms whose own tests this machine cannot cover."""
    if platform == "win32":
        return ["macOS"]
    if platform == "darwin":
        return ["Windows"]
    return ["Windows", "macOS"]

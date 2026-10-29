"""Filesystem locations used by Jarvis."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from platformdirs import user_data_dir

LOG = logging.getLogger(__name__)

APP_NAME = "Jarvis"
DATABASE_FILENAME = "memory.sqlite"

# The default before it became per-user, relative to the working directory of `jarvis daemon`.
LEGACY_DATABASE_PATH = Path(".jarvis/dev/memory.sqlite")

_WINDOWS_ENV_VAR = re.compile(r"%([^%]+)%")


class DatabaseLocationError(RuntimeError):
    """Raised instead of creating an empty database while an existing one is elsewhere."""


def default_data_dir() -> Path:
    r"""The per-user data directory.

    Windows: %APPDATA%\Jarvis. macOS: ~/Library/Application Support/Jarvis.
    Linux: $XDG_DATA_HOME/Jarvis (default ~/.local/share/Jarvis).
    """
    return Path(user_data_dir(APP_NAME, appauthor=False, roaming=True))


def default_database_path() -> Path:
    return default_data_dir() / DATABASE_FILENAME


def expand_env_vars(text: str, windows: bool = os.name == "nt") -> str:
    """Expand `%VAR%` on Windows and `$VAR` / `${VAR}` elsewhere; unknown names stay as is.

    On Windows `$` is an ordinary path character, so only the `%VAR%` form is expanded.
    """
    if not windows:
        return os.path.expandvars(text)
    return _WINDOWS_ENV_VAR.sub(lambda m: os.environ.get(m.group(1), m.group(0)), text)


def expand_user_and_vars(path: str | Path) -> Path:
    """`path` with `~` and environment variables expanded (see expand_env_vars)."""
    return Path(expand_env_vars(os.path.expanduser(str(path))))


def expand_path(path: str | Path) -> Path:
    """Absolute, expanded path.

    A relative path is resolved against the current working directory, as it always was.
    """
    return Path(os.path.abspath(expand_user_and_vars(path)))


def source_root() -> Path:
    """The directory containing the `jarvis` package: the repository root in a checkout."""
    return Path(__file__).resolve().parents[2]


def find_legacy_databases(cwd: Path | None = None) -> list[Path]:
    """Existing databases at the previous default location.

    That location depended on the working directory. The README runs Jarvis from the
    repository root, so the root is checked as well as the current directory.
    """
    found: list[Path] = []
    for base in (cwd or Path.cwd(), source_root()):
        candidate = Path(os.path.abspath(base / LEGACY_DATABASE_PATH))
        if candidate.is_file() and candidate not in found:
            found.append(candidate)
    return found


def check_default_database_location(database_path: Path, cwd: Path | None = None) -> None:
    """Refuse to start on a new, empty default database while an earlier one exists.

    Neither file is created, moved or modified here; the error explains how to keep using
    the earlier database.
    """
    legacy = [
        path for path in find_legacy_databases(cwd)
        if os.path.normcase(path) != os.path.normcase(database_path)
    ]
    if not legacy:
        return

    listed = ", ".join(str(path) for path in legacy)
    if database_path.exists():
        LOG.warning(
            f"Using the database at {database_path}. An older database at the previous default "
            f"location is not used: {listed}"
        )
        return

    name = database_path.name
    raise DatabaseLocationError(
        f"No database exists at the default location {database_path}, but an existing Jarvis "
        f"database was found at the previous default location (relative to the working "
        f"directory): {listed}. Jarvis will not create a new, empty database. To keep using "
        f"it, set JARVIS_DATABASE_PATH (or [memory] database_path in the config file) to its "
        f"path, or stop Jarvis and move {name} together with any {name}-wal and {name}-shm "
        f"files into {database_path.parent}."
    )

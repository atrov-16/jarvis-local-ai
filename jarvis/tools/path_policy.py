"""Shared workspace path policy for every tool that touches the filesystem.

A path is authorised only if, after canonicalisation (symlinks resolved, `..`
collapsed), it lies inside an enabled workspace and none of its segments below the
workspace root are protected. Protected segments are matched case-insensitively,
because Windows and default macOS filesystems are case-insensitive.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Directories whose contents tools may never read, write, list or delete.
PROTECTED_DIR_NAMES = frozenset({".git", ".jarvis", ".jarvis_backups"})
# `.env` and `.env.*` hold secrets; these conventional templates do not.
ENV_TEMPLATE_NAMES = frozenset({".env.example", ".env.sample", ".env.template", ".env.dist"})


class ProtectedPathError(PermissionError):
    """The path is inside the workspace but refers to protected content."""


@dataclass(frozen=True)
class AuthorizedPath:
    path: Path  # Canonical absolute path.
    root: Path  # Canonical root of the workspace that authorised it.

    @property
    def is_root(self) -> bool:
        return self.path == self.root


def workspace_roots(workspaces: list[dict[str, Any]]) -> list[Path]:
    """Canonical roots of the enabled workspaces (disabled ones grant no access)."""
    roots = []
    for ws in workspaces:
        if "enabled" in ws and not ws["enabled"]:
            continue
        roots.append(Path(str(ws["path"])).resolve())
    return roots


def resolve_in_workspace(path_str: str, workspaces: list[dict[str, Any]]) -> AuthorizedPath:
    """Resolve `path_str` (relative to a workspace root, or absolute) inside a workspace.

    Raises PermissionError if it does not land inside any enabled workspace.
    """
    target = Path(path_str).expanduser()
    for root in workspace_roots(workspaces):
        candidate = target.resolve() if target.is_absolute() else (root / target).resolve()
        if candidate.is_relative_to(root):
            return AuthorizedPath(candidate, root)
    raise PermissionError(f"Path is outside allowed workspaces: {path_str}")


def protected_segment(authorized: AuthorizedPath) -> str | None:
    """Return the protected segment of the path, if any."""
    for part in authorized.path.relative_to(authorized.root).parts:
        name = part.casefold()
        if name in PROTECTED_DIR_NAMES:
            return part
        if name == ".env" or (name.startswith(".env.") and name not in ENV_TEMPLATE_NAMES):
            return part
    return None


def authorize_path(path_str: str, workspaces: list[dict[str, Any]]) -> AuthorizedPath:
    """Resolve inside a workspace and reject protected content (secrets, VCS, Jarvis data)."""
    authorized = resolve_in_workspace(path_str, workspaces)
    segment = protected_segment(authorized)
    if segment is not None:
        raise ProtectedPathError(f"Access Denied: '{segment}' is a protected system path.")
    return authorized


def is_protected(path: Path, root: Path) -> bool:
    """True if a path already known to be inside `root` refers to protected content."""
    return protected_segment(AuthorizedPath(path, root)) is not None

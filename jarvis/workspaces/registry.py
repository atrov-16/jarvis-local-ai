"""Workspace registry for filesystem permission boundaries."""

from __future__ import annotations

import os
from pathlib import Path

from jarvis.storage.unit_of_work import UnitOfWork


def _system_directories() -> tuple[list[Path], list[Path]]:
    """(directories that may not contain a workspace, directories that may not be one)."""
    if os.name == "nt":
        env_dirs = ("SYSTEMROOT", "WINDIR", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA")
        no_workspace_inside = [Path(os.environ[v]) for v in env_dirs if os.environ.get(v)]
        not_a_workspace = [Path(os.environ.get("SYSTEMDRIVE", "C:") + "\\Users")]
    else:
        no_workspace_inside = [Path(p) for p in (
            "/bin", "/sbin", "/usr", "/etc", "/boot", "/dev", "/proc", "/sys", "/lib", "/lib64",
            "/System", "/Library", "/private/etc",
        )]
        not_a_workspace = [Path(p) for p in (
            "/var", "/private", "/private/var", "/opt", "/tmp", "/private/tmp", "/home",
            "/Users", "/Applications", "/Volumes", "/mnt", "/media", "/srv", "/root",
        )]
    return [p.resolve() for p in no_workspace_inside], [p.resolve() for p in not_a_workspace]


def dangerous_root_reason(path: Path) -> str | None:
    """Why `path` (already resolved) must not become a workspace root, if it must not."""
    if path == Path(path.anchor):
        return "a filesystem root"
    home = Path.home().resolve()
    if path == home or home.is_relative_to(path):
        return "the home directory or one of its parents"
    no_workspace_inside, not_a_workspace = _system_directories()
    for system_dir in no_workspace_inside:
        if path == system_dir or path.is_relative_to(system_dir):
            return f"inside system directory {system_dir}"
    if path in not_a_workspace:
        return "a top-level system directory"
    return None


class WorkspaceRegistry:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def add(self, name: str, path: str) -> str:
        # Normalize path
        resolved = Path(path).expanduser().resolve()
        reason = dangerous_root_reason(resolved)
        if reason:
            raise ValueError(f"Refusing to register {resolved} as a workspace: it is {reason}.")
        # A workspace may be registered before it exists, but never as a file.
        if resolved.exists() and not resolved.is_dir():
            raise ValueError(f"Workspace path is not a directory: {resolved}")
        normalized_path = str(resolved)

        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            # Check for existing workspace with same normalized path
            existing = await unit.repositories.workspaces.get_by_path(normalized_path)
            if existing:
                raise ValueError(f"Workspace already registered at path: {normalized_path}")

            workspace_id = await unit.repositories.workspaces.insert(
                name=name, path=normalized_path
            )
            await unit.repositories.audit.insert(
                actor="system",
                action_type="workspace.add",
                summary=f"Added workspace: {name} ({normalized_path})",
                target=workspace_id,
            )
            return workspace_id

    async def get(self, workspace_id: str) -> dict[str, object] | None:
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            return await unit.repositories.workspaces.get(workspace_id)

    async def get_by_path(self, path: str) -> dict[str, object] | None:
        normalized_path = str(Path(path).resolve())
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            return await unit.repositories.workspaces.get_by_path(normalized_path)

    async def list(self) -> list[dict[str, object]]:
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            return await unit.repositories.workspaces.list()

    async def update(self, workspace_id: str, **kwargs: object) -> bool:
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            workspace = await unit.repositories.workspaces.get(workspace_id)
            if not workspace:
                return False
            
            updated = await unit.repositories.workspaces.update(workspace_id, **kwargs)
            if updated:
                await unit.repositories.audit.insert(
                    actor="system",
                    action_type="workspace.update",
                    summary=f"Updated workspace: {workspace['name']}",
                    target=workspace_id,
                    details=kwargs,
                )
            return updated

    async def remove(self, workspace_id: str) -> bool:
        async with self._uow.begin() as unit:
            assert unit.repositories is not None
            workspace = await unit.repositories.workspaces.get(workspace_id)
            if not workspace:
                return False
            
            deleted = await unit.repositories.workspaces.delete(workspace_id)
            if deleted:
                await unit.repositories.audit.insert(
                    actor="system",
                    action_type="workspace.remove",
                    summary=f"Removed workspace: {workspace['name']}",
                    target=workspace_id,
                )
            return deleted

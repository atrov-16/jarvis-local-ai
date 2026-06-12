"""Workspace registry for filesystem permission boundaries."""

from __future__ import annotations

from pathlib import Path

from jarvis.storage.unit_of_work import UnitOfWork


class WorkspaceRegistry:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def add(self, name: str, path: str) -> str:
        # Normalize path
        normalized_path = str(Path(path).resolve())
        
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

"""Project registry for logical project management."""

from __future__ import annotations

from jarvis.storage.unit_of_work import UnitOfWork

CURRENT_PROJECT_KEY = "current_project_id"


class ProjectRegistry:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def create(self, name: str, description: str | None = None) -> str:
        async with self._uow as unit:
            assert unit.repositories is not None
            project_id = await unit.repositories.projects.insert(
                name=name, description=description
            )
            await unit.repositories.audit.insert(
                actor="system",
                action_type="project.create",
                summary=f"Created project: {name}",
                target=project_id,
            )
            return project_id

    async def list(self) -> list[dict[str, object]]:
        async with self._uow as unit:
            assert unit.repositories is not None
            return await unit.repositories.projects.list()

    async def get(self, project_id: str) -> dict[str, object] | None:
        async with self._uow as unit:
            assert unit.repositories is not None
            return await unit.repositories.projects.get(project_id)

    async def get_by_name(self, name: str) -> dict[str, object] | None:
        async with self._uow as unit:
            assert unit.repositories is not None
            return await unit.repositories.projects.get_by_name(name)

    async def update(self, project_id: str, **kwargs: object) -> bool:
        async with self._uow as unit:
            assert unit.repositories is not None
            project = await unit.repositories.projects.get(project_id)
            if not project:
                return False
            
            updated = await unit.repositories.projects.update(project_id, **kwargs)
            if updated:
                await unit.repositories.audit.insert(
                    actor="system",
                    action_type="project.update",
                    summary=f"Updated project: {project['name']}",
                    target=project_id,
                    details=kwargs,
                )
            return updated

    async def delete(self, project_id: str) -> bool:
        async with self._uow as unit:
            assert unit.repositories is not None
            project = await unit.repositories.projects.get(project_id)
            if not project:
                return False

            # Check if this is the current project
            state = await unit.repositories.app_state.get(CURRENT_PROJECT_KEY)
            if state and state.get("id") == project_id:
                await unit.repositories.app_state.set(CURRENT_PROJECT_KEY, {})
                await unit.repositories.audit.insert(
                    actor="system",
                    action_type="project.switch",
                    summary=f"Cleared current project selection because project {project_id} was deleted",
                )

            deleted = await unit.repositories.projects.delete(project_id)
            if deleted:
                await unit.repositories.audit.insert(
                    actor="system",
                    action_type="project.delete",
                    summary=f"Deleted project: {project['name']}",
                    target=project_id,
                )
            return deleted

    async def get_current_id(self) -> str | None:
        async with self._uow as unit:
            assert unit.repositories is not None
            state = await unit.repositories.app_state.get(CURRENT_PROJECT_KEY)
            return str(state["id"]) if state and "id" in state else None

    async def switch_current(self, project_id: str | None) -> None:
        async with self._uow as unit:
            assert unit.repositories is not None
            if project_id:
                project = await unit.repositories.projects.get(project_id)
                if not project:
                    raise ValueError(f"Project not found: {project_id}")
                
                await unit.repositories.app_state.set(CURRENT_PROJECT_KEY, {"id": project_id})
                await unit.repositories.audit.insert(
                    actor="system",
                    action_type="project.switch",
                    summary=f"Switched to project: {project['name']}",
                    target=project_id,
                )
            else:
                await unit.repositories.app_state.set(CURRENT_PROJECT_KEY, {})
                await unit.repositories.audit.insert(
                    actor="system",
                    action_type="project.switch",
                    summary="Cleared current project selection",
                )

    async def link_workspace(self, project_id: str, workspace_id: str) -> None:
        async with self._uow as unit:
            assert unit.repositories is not None
            await unit.repositories.projects.link_workspace(project_id, workspace_id)
            await unit.repositories.audit.insert(
                actor="system",
                action_type="project.link_workspace",
                summary=f"Linked workspace {workspace_id} to project {project_id}",
                target=project_id,
                details={"workspace_id": workspace_id},
            )

    async def unlink_workspace(self, project_id: str, workspace_id: str) -> bool:
        async with self._uow as unit:
            assert unit.repositories is not None
            unlinked = await unit.repositories.projects.unlink_workspace(project_id, workspace_id)
            if unlinked:
                await unit.repositories.audit.insert(
                    actor="system",
                    action_type="project.unlink_workspace",
                    summary=f"Unlinked workspace {workspace_id} from project {project_id}",
                    target=project_id,
                    details={"workspace_id": workspace_id},
                )
            return unlinked

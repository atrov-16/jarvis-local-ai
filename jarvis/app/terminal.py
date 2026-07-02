"""Typer terminal client for Jarvis Phase 0."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from jarvis.app.daemon import run_daemon
from jarvis.config.manager import load_config
from jarvis.config.secrets import SecretManager

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = typer.Typer(help="Jarvis local assistant.")
config_app = typer.Typer(help="Configuration commands.")
workspace_app = typer.Typer(help="Workspace management.")
project_app = typer.Typer(help="Project management.")
memory_app = typer.Typer(help="Memory management.")
task_app = typer.Typer(help="Task execution and planning.")
console = Console()


def _get_api_client(config_path: Path | None = None) -> httpx.Client:
    jarvis_config = load_config(config_path)
    secret_manager = SecretManager()
    base_url = f"http://{jarvis_config.server.host}:{jarvis_config.server.port}"
    token = secret_manager.get_api_token()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(base_url=base_url, headers=headers, timeout=10.0)


def _handle_api_error(e: Exception) -> None:
    if isinstance(e, httpx.HTTPStatusError):
        if e.response.status_code == 401:
            console.print(Panel("Unauthorized: Valid local API token required.", title="Error", style="red"))
        elif e.response.status_code == 400:
            console.print(Panel(f"Bad Request: {e.response.json().get('detail', 'Unknown error')}", title="Error", style="red"))
        else:
            console.print(Panel(f"API Error: {e.response.status_code} - {e.response.text}", title="Error", style="red"))
    else:
        console.print(Panel("Jarvis daemon is not reachable. Start it with `jarvis daemon`.", title="Error", style="red"))


@app.command()
def daemon(
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to a Jarvis config TOML file."),
    ] = None,
) -> None:
    """Start the local Jarvis daemon."""
    run_daemon(config)


@app.command()
def status(
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to a Jarvis config TOML file."),
    ] = None,
) -> None:
    """Show daemon status."""
    with _get_api_client(config) as client:
        try:
            resp_status = client.get("/v1/status")
            resp_status.raise_for_status()
            status_data = resp_status.json()

            resp_projects = client.get("/v1/projects")
            resp_projects.raise_for_status()
            projects = resp_projects.json()

            resp_current = client.get("/v1/projects/current")
            resp_current.raise_for_status()
            current_project_id = resp_current.json()["id"]

            resp_workspaces = client.get("/v1/workspaces")
            resp_workspaces.raise_for_status()
            workspaces = resp_workspaces.json()

            current_project_name = "None"
            if current_project_id:
                for p in projects:
                    if p["id"] == current_project_id:
                        current_project_name = p["name"]
                        break

            table = Table(title="Jarvis Status", show_header=False)
            table.add_row("Status", "[green]Online[/green]")
            table.add_row("Version", status_data["version"])
            table.add_row("Current Project", f"[cyan]{current_project_name}[/cyan]")
            table.add_row("Projects", str(len(projects)))
            table.add_row("Workspaces", str(len(workspaces)))
            
            # Providers
            for p in status_data.get("providers", []):
                p_status = "[green]Ready[/green]" if p["available"] else f"[red]Error: {p['error']}[/red]"
                table.add_row(f"Provider: {p['name']}", p_status)
                
            console.print(table)

        except Exception as e:
            _handle_api_error(e)


@workspace_app.command("add")
def workspace_add(
    path: str,
    name: str = typer.Option(None, "--name", "-n", help="Optional name for the workspace."),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Register a new workspace."""
    if not name:
        name = Path(path).name
    
    with _get_api_client(config) as client:
        try:
            resp = client.post("/v1/workspaces", json={"name": name, "path": path})
            resp.raise_for_status()
            console.print(f"[green]Registered workspace:[/green] {name} ({resp.json()['path']})")
        except Exception as e:
            _handle_api_error(e)


@workspace_app.command("list")
def workspace_list(config: Path | None = typer.Option(None, "--config", "-c")) -> None:
    """List all registered workspaces."""
    with _get_api_client(config) as client:
        try:
            resp = client.get("/v1/workspaces")
            resp.raise_for_status()
            workspaces = resp.json()

            if not workspaces:
                console.print("No workspaces registered.")
                return

            table = Table(title="Workspaces")
            table.add_column("ID", style="dim")
            table.add_column("Name", style="cyan")
            table.add_column("Path")
            table.add_column("Status")

            for w in workspaces:
                status_str = "[green]Enabled[/green]" if w["enabled"] else "[red]Disabled[/red]"
                table.add_row(w["id"][:8], w["name"], w["path"], status_str)
            console.print(table)
        except Exception as e:
            _handle_api_error(e)


@workspace_app.command("remove")
def workspace_remove(
    workspace_id: str, config: Path | None = typer.Option(None, "--config", "-c")
) -> None:
    """Remove a workspace."""
    with _get_api_client(config) as client:
        try:
            resp = client.delete(f"/v1/workspaces/{workspace_id}")
            resp.raise_for_status()
            console.print(f"[green]Removed workspace:[/green] {workspace_id}")
        except Exception as e:
            # Try to resolve by name if ID fails? (Future enhancement)
            _handle_api_error(e)


@project_app.command("create")
def project_create(
    name: str,
    description: str = typer.Option(None, "--description", "-d"),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Create a new project."""
    with _get_api_client(config) as client:
        try:
            resp = client.post("/v1/projects", json={"name": name, "description": description})
            resp.raise_for_status()
            console.print(f"[green]Created project:[/green] {name}")
        except Exception as e:
            _handle_api_error(e)


@project_app.command("list")
def project_list(config: Path | None = typer.Option(None, "--config", "-c")) -> None:
    """List all projects."""
    with _get_api_client(config) as client:
        try:
            resp_projects = client.get("/v1/projects")
            resp_projects.raise_for_status()
            projects = resp_projects.json()

            resp_current = client.get("/v1/projects/current")
            resp_current.raise_for_status()
            current_id = resp_current.json()["id"]

            if not projects:
                console.print("No projects found.")
                return

            table = Table(title="Projects")
            table.add_column("Current", justify="center")
            table.add_column("ID", style="dim")
            table.add_column("Name", style="cyan")
            table.add_column("Status")

            for p in projects:
                is_current = "*" if p["id"] == current_id else ""
                table.add_row(is_current, p["id"][:8], p["name"], p["status"])
            console.print(table)
        except Exception as e:
            _handle_api_error(e)


@project_app.command("switch")
@project_app.command("use")
@project_app.command("select")
def project_switch(
    name: str, config: Path | None = typer.Option(None, "--config", "-c")
) -> None:
    """Switch to a different project."""
    with _get_api_client(config) as client:
        try:
            # Resolve name to ID
            resp_projects = client.get("/v1/projects")
            resp_projects.raise_for_status()
            projects = resp_projects.json()

            project_id = None
            for p in projects:
                if p["name"].lower() == name.lower() or p["id"].startswith(name):
                    project_id = p["id"]
                    name = p["name"]
                    break
            
            if not project_id:
                console.print(f"[red]Project not found:[/red] {name}")
                return

            resp = client.post("/v1/projects/current", json={"id": project_id})
            resp.raise_for_status()
            console.print(f"[green]Switched to project:[/green] {name}")
        except Exception as e:
            _handle_api_error(e)


@config_app.command("show")
def config_show(
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to a Jarvis config TOML file."),
    ] = None,
) -> None:
    """Show public, non-secret configuration."""
    jarvis_config = load_config(config)
    console.print_json(data=jarvis_config.public_dict())


@memory_app.command("search")
def memory_search(
    query: str,
    project: str = typer.Option(None, "--project", "-p", help="Filter by project name or ID."),
    limit: int = typer.Option(20, "--limit", "-l"),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Search long-term memory."""
    with _get_api_client(config) as client:
        try:
            params = {"q": query, "limit": limit}
            if project:
                # Resolve project name to ID if needed
                resp_projects = client.get("/v1/projects")
                resp_projects.raise_for_status()
                for p in resp_projects.json():
                    if p["name"].lower() == project.lower() or p["id"].startswith(project):
                        params["project_id"] = p["id"]
                        break

            resp = client.get("/v1/memory/search", params=params)
            resp.raise_for_status()
            results = resp.json()

            if not results:
                console.print(f"No memories found matching: [bold]{query}[/bold]")
                return

            table = Table(title=f"Memory Search: {query}")
            table.add_column("Score", justify="right", style="dim")
            table.add_column("Type", style="cyan")
            table.add_column("Title/Content")
            
            for r in results:
                title = r["title"] or ""
                content = r["content"]
                display = f"[bold]{title}[/bold]\n{content}" if title else content
                table.add_row(f"{r['relevance_score']:.2f}", r["memory_type"], display)
            console.print(table)
        except Exception as e:
            _handle_api_error(e)


@memory_app.command("proposals")
def memory_proposals(config: Path | None = typer.Option(None, "--config", "-c")) -> None:
    """List pending memory proposals."""
    with _get_api_client(config) as client:
        try:
            resp = client.get("/v1/memory/proposals")
            resp.raise_for_status()
            proposals = resp.json()

            if not proposals:
                console.print("No pending memory proposals.")
                return

            table = Table(title="Pending Memory Proposals")
            table.add_column("ID", style="dim")
            table.add_column("Type", style="cyan")
            table.add_column("Proposed Content")
            table.add_column("Reason", style="italic")

            for p in proposals:
                table.add_row(p["id"][:8], p["memory_type"], p["proposed_content"], p["reason"])
            console.print(table)
        except Exception as e:
            _handle_api_error(e)


@memory_app.command("approve")
def memory_approve(
    proposal_id: str,
    title: str = typer.Option(None, "--title", "-t", help="Optional title for the memory."),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Approve a memory proposal."""
    with _get_api_client(config) as client:
        try:
            # Try to resolve short ID
            if len(proposal_id) < 36:
                resp_p = client.get("/v1/memory/proposals")
                resp_p.raise_for_status()
                for p in resp_p.json():
                    if p["id"].startswith(proposal_id):
                        proposal_id = p["id"]
                        break

            resp = client.post(f"/v1/memory/proposals/{proposal_id}/approve", json={"title": title})
            resp.raise_for_status()
            console.print(f"[green]Approved memory proposal:[/green] {proposal_id[:8]}")
        except Exception as e:
            _handle_api_error(e)


@memory_app.command("deny")
def memory_deny(
    proposal_id: str,
    reason: str = typer.Option(None, "--reason", "-r"),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Deny a memory proposal."""
    with _get_api_client(config) as client:
        try:
            # Try to resolve short ID
            if len(proposal_id) < 36:
                resp_p = client.get("/v1/memory/proposals")
                resp_p.raise_for_status()
                for p in resp_p.json():
                    if p["id"].startswith(proposal_id):
                        proposal_id = p["id"]
                        break

            resp = client.post(f"/v1/memory/proposals/{proposal_id}/deny", json={"reason": reason})
            resp.raise_for_status()
            console.print(f"[green]Denied memory proposal:[/green] {proposal_id[:8]}")
        except Exception as e:
            _handle_api_error(e)


@memory_app.command("list")
def memory_list(
    project: str = typer.Option(None, "--project", "-p"),
    limit: int = typer.Option(50, "--limit", "-l"),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """List long-term memories."""
    with _get_api_client(config) as client:
        try:
            params = {"limit": limit}
            if project:
                resp_projects = client.get("/v1/projects")
                resp_projects.raise_for_status()
                for p in resp_projects.json():
                    if p["name"].lower() == project.lower() or p["id"].startswith(project):
                        params["project_id"] = p["id"]
                        break

            resp = client.get("/v1/memory/long-term", params=params)
            resp.raise_for_status()
            memories = resp.json()

            if not memories:
                console.print("No long-term memories found.")
                return

            table = Table(title="Long-Term Memories")
            table.add_column("ID", style="dim")
            table.add_column("Type", style="cyan")
            table.add_column("Title")
            table.add_column("Content", max_width=60)

            for m in memories:
                table.add_row(m["id"][:8], m["memory_type"], m["title"] or "", m["content"])
            console.print(table)
        except Exception as e:
            _handle_api_error(e)


@memory_app.command("remove")
def memory_remove(
    memory_id: str, config: Path | None = typer.Option(None, "--config", "-c")
) -> None:
    """Remove a long-term memory."""
    with _get_api_client(config) as client:
        try:
            # Try to resolve short ID
            if len(memory_id) < 36:
                resp_m = client.get("/v1/memory/long-term")
                resp_m.raise_for_status()
                for m in resp_m.json():
                    if m["id"].startswith(memory_id):
                        memory_id = m["id"]
                        break

            resp = client.delete(f"/v1/memory/long-term/{memory_id}")
            resp.raise_for_status()
            console.print(f"[green]Removed memory:[/green] {memory_id[:8]}")
        except Exception as e:
            _handle_api_error(e)


@task_app.command("submit")
def task_submit(
    request: str,
    project: str = typer.Option(None, "--project", "-p", help="Filter by project name or ID."),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Submit a new task."""
    with _get_api_client(config) as client:
        try:
            params = {"user_request": request}
            if project:
                resp_projects = client.get("/v1/projects")
                resp_projects.raise_for_status()
                for p in resp_projects.json():
                    if p["name"].lower() == project.lower() or p["id"].startswith(project):
                        params["project_id"] = p["id"]
                        break

            resp = client.post("/v1/tasks", json=params)
            resp.raise_for_status()
            task_id = resp.json()["id"]
            console.print(f"[green]Task submitted:[/green] {task_id[:8]}")
        except Exception as e:
            _handle_api_error(e)


@task_app.command("list")
def task_list(
    status: str = typer.Option(None, "--status", "-s"),
    limit: int = typer.Option(50, "--limit", "-l"),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """List tasks."""
    with _get_api_client(config) as client:
        try:
            params = {"limit": limit}
            if status:
                params["status"] = status
            resp = client.get("/v1/tasks", params=params)
            resp.raise_for_status()
            tasks = resp.json()

            if not tasks:
                console.print("No tasks found.")
                return

            table = Table(title="Tasks")
            table.add_column("ID", style="dim")
            table.add_column("Status", style="cyan")
            table.add_column("Title")

            for t in tasks:
                table.add_row(t["id"][:8], t["status"], t["title"])
            console.print(table)
        except Exception as e:
            _handle_api_error(e)


@task_app.command("status")
def task_status(
    task_id: str,
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Get task status and steps."""
    with _get_api_client(config) as client:
        try:
            if len(task_id) < 36:
                resp_t = client.get("/v1/tasks")
                resp_t.raise_for_status()
                for t in resp_t.json():
                    if t["id"].startswith(task_id):
                        task_id = t["id"]
                        break

            resp = client.get(f"/v1/tasks/{task_id}")
            resp.raise_for_status()
            task = resp.json()

            console.print(f"[bold]Task:[/bold] {task['title']} ({task['id'][:8]})")
            console.print(f"[bold]Status:[/bold] {task['status']}")
            
            steps = task.get("steps", [])
            if not steps:
                console.print("No steps yet.")
                return

            table = Table(title="Steps")
            table.add_column("#", justify="right", style="dim")
            table.add_column("Status", style="cyan")
            table.add_column("Title")
            
            for s in steps:
                table.add_row(str(s["step_index"]), s["status"], s["title"])
            console.print(table)
        except Exception as e:
            _handle_api_error(e)


@task_app.command("approve")
def task_approve(
    task_id: str,
    step: str | None = typer.Option(None, "--step", "-s", help="Approve a specific step (tool approval)."),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Approve a task plan or tool execution step."""
    with _get_api_client(config) as client:
        try:
            if len(task_id) < 36:
                resp_t = client.get("/v1/tasks")
                resp_t.raise_for_status()
                for t in resp_t.json():
                    if t["id"].startswith(task_id):
                        task_id = t["id"]
                        break

            if step:
                if len(step) < 36:
                    resp_s = client.get(f"/v1/tasks/{task_id}")
                    resp_s.raise_for_status()
                    for s in resp_s.json().get("steps", []):
                        if s["id"].startswith(step):
                            step = s["id"]
                            break

                resp = client.post(f"/v1/tasks/{task_id}/steps/{step}/approve", json={"reason": None})
                resp.raise_for_status()
                console.print(f"[green]Task step approved:[/green] {step[:8]}")
            else:
                resp = client.post(f"/v1/tasks/{task_id}/plan/approve")
                resp.raise_for_status()
                console.print(f"[green]Task plan approved:[/green] {task_id[:8]}")
        except Exception as e:
            _handle_api_error(e)

@task_app.command("deny")
def task_deny(
    task_id: str,
    step: str = typer.Option(..., "--step", "-s", help="Deny a specific step (tool approval)."),
    reason: str | None = typer.Option(None, "--reason", "-r", help="Reason for denying the step."),
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Deny a tool execution step."""
    with _get_api_client(config) as client:
        try:
            if len(task_id) < 36:
                resp_t = client.get("/v1/tasks")
                resp_t.raise_for_status()
                for t in resp_t.json():
                    if t["id"].startswith(task_id):
                        task_id = t["id"]
                        break
            
            if len(step) < 36:
                resp_s = client.get(f"/v1/tasks/{task_id}")
                resp_s.raise_for_status()
                for s in resp_s.json().get("steps", []):
                    if s["id"].startswith(step):
                        step = s["id"]
                        break

            resp = client.post(f"/v1/tasks/{task_id}/steps/{step}/deny", json={"reason": reason})
            resp.raise_for_status()
            console.print(f"[yellow]Task step denied:[/yellow] {step[:8]}")
        except Exception as e:
            _handle_api_error(e)


@task_app.command("resume")
def task_resume(
    task_id: str,
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Resume a paused task."""
    with _get_api_client(config) as client:
        try:
            if len(task_id) < 36:
                resp_t = client.get("/v1/tasks")
                resp_t.raise_for_status()
                for t in resp_t.json():
                    if t["id"].startswith(task_id):
                        task_id = t["id"]
                        break

            resp = client.post(f"/v1/tasks/{task_id}/resume")
            resp.raise_for_status()
            console.print(f"[green]Task resumed:[/green] {task_id[:8]}")
        except Exception as e:
            _handle_api_error(e)


@task_app.command("cancel")
def task_cancel(
    task_id: str,
    config: Path | None = typer.Option(None, "--config", "-c"),
) -> None:
    """Cancel a task."""
    with _get_api_client(config) as client:
        try:
            if len(task_id) < 36:
                resp_t = client.get("/v1/tasks")
                resp_t.raise_for_status()
                for t in resp_t.json():
                    if t["id"].startswith(task_id):
                        task_id = t["id"]
                        break

            resp = client.post(f"/v1/tasks/{task_id}/cancel")
            resp.raise_for_status()
            console.print(f"[green]Task cancelled:[/green] {task_id[:8]}")
        except Exception as e:
            _handle_api_error(e)


app.add_typer(config_app, name="config")
app.add_typer(workspace_app, name="workspace")
app.add_typer(project_app, name="project")
app.add_typer(memory_app, name="memory")
app.add_typer(task_app, name="task")


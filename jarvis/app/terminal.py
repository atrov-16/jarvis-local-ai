"""Typer terminal client for Jarvis Phase 0."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.panel import Panel

from jarvis.app.daemon import run_daemon
from jarvis.config.manager import load_config
from jarvis.config.secrets import SecretManager

app = typer.Typer(help="Jarvis local assistant.")
config_app = typer.Typer(help="Configuration commands.")
console = Console()


def _auth_headers(secret_manager: SecretManager) -> dict[str, str]:
    token = secret_manager.get_api_token()
    if token is None:
        return {}
    return {"Authorization": f"Bearer {token}"}


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
    jarvis_config = load_config(config)
    secret_manager = SecretManager()
    base_url = f"http://{jarvis_config.server.host}:{jarvis_config.server.port}"

    try:
        response = httpx.get(
            f"{base_url}/v1/status",
            headers=_auth_headers(secret_manager),
            timeout=5.0,
        )
    except httpx.HTTPError:
        console.print(
            Panel(
                "Jarvis daemon is not reachable yet. Start it with `jarvis daemon`.",
                title="Status",
            )
        )
        return

    if response.status_code == 401:
        console.print(
            Panel(
                "Jarvis daemon requires a valid local API token. Set JARVIS_API_TOKEN.",
                title="Status",
            )
        )
        return

    response.raise_for_status()
    console.print_json(data=response.json())


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


app.add_typer(config_app, name="config")


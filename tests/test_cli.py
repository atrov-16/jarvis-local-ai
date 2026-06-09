from __future__ import annotations

from typer.testing import CliRunner

from jarvis.app.terminal import app


def test_cli_imports_and_shows_help() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Jarvis local assistant" in result.output


def test_status_handles_unavailable_daemon(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SERVER_PORT", "65534")
    runner = CliRunner()
    result = runner.invoke(app, ["status"])

    assert result.exit_code == 0
    assert "not reachable" in result.output

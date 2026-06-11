from __future__ import annotations

from unittest.mock import MagicMock, patch
from typer.testing import CliRunner
from jarvis.app.terminal import app

def test_workspace_commands(monkeypatch) -> None:
    runner = CliRunner()
    
    with patch("httpx.Client") as mock_client_class:
        mock_client = mock_client_class.return_value.__enter__.return_value
        
        # Test workspace add
        mock_client.post.return_value = MagicMock(status_code=200)
        mock_client.post.return_value.json.return_value = {"id": "w1", "path": "/test"}
        result = runner.invoke(app, ["workspace", "add", "/test", "--name", "W1"])
        assert result.exit_code == 0
        assert "Registered workspace: W1" in result.output

        # Test workspace list
        mock_client.get.return_value = MagicMock(status_code=200)
        mock_client.get.return_value.json.return_value = [
            {"id": "w1", "name": "W1", "path": "/test", "enabled": True}
        ]
        result = runner.invoke(app, ["workspace", "list"])
        assert result.exit_code == 0
        assert "W1" in result.output
        assert "/test" in result.output

        # Test workspace remove
        mock_client.delete.return_value = MagicMock(status_code=200)
        result = runner.invoke(app, ["workspace", "remove", "w1"])
        assert result.exit_code == 0
        assert "Removed workspace: w1" in result.output

def test_project_commands() -> None:
    runner = CliRunner()
    
    with patch("httpx.Client") as mock_client_class:
        mock_client = mock_client_class.return_value.__enter__.return_value
        
        # Test project create
        mock_client.post.return_value = MagicMock(status_code=200)
        result = runner.invoke(app, ["project", "create", "P1"])
        assert result.exit_code == 0
        assert "Created project: P1" in result.output

        # Test project list
        mock_client.get.side_effect = [
            MagicMock(status_code=200, json=lambda: [{"id": "p1", "name": "P1", "status": "active"}]),
            MagicMock(status_code=200, json=lambda: {"id": "p1"})
        ]
        result = runner.invoke(app, ["project", "list"])
        assert result.exit_code == 0
        assert "*" in result.output # Current project indicator
        assert "P1" in result.output

        # Test project switch
        mock_client.get.side_effect = [
            MagicMock(status_code=200, json=lambda: [{"id": "p1", "name": "P1", "status": "active"}]),
        ]
        mock_client.post.return_value = MagicMock(status_code=200)
        result = runner.invoke(app, ["project", "switch", "P1"])
        assert result.exit_code == 0
        assert "Switched to project: P1" in result.output

def test_status_command_extension() -> None:
    runner = CliRunner()
    
    with patch("httpx.Client") as mock_client_class:
        mock_client = mock_client_class.return_value.__enter__.return_value
        
        mock_client.get.side_effect = [
            MagicMock(status_code=200, json=lambda: {"version": "0.1.0"}), # /v1/status
            MagicMock(status_code=200, json=lambda: [{"id": "p1", "name": "P1"}]), # /v1/projects
            MagicMock(status_code=200, json=lambda: {"id": "p1"}), # /v1/projects/current
            MagicMock(status_code=200, json=lambda: [{"id": "w1"}]), # /v1/workspaces
        ]
        
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "Online" in result.output
        assert "P1" in result.output
        assert "Projects" in result.output
        assert "1" in result.output # Project count
        assert "Workspaces" in result.output
        assert "1" in result.output # Workspace count

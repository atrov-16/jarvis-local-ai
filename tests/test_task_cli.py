"""Tests for Task CLI commands."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from jarvis.app.terminal import app

runner = CliRunner()

@patch("jarvis.app.terminal._get_api_client")
def test_task_submit(mock_client_ctx: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    
    # Mock /v1/projects to resolve project name
    mock_proj_resp = MagicMock()
    mock_proj_resp.json.return_value = [{"id": "proj-1", "name": "test-proj"}]
    mock_client.get.return_value = mock_proj_resp

    # Mock /v1/tasks
    mock_task_resp = MagicMock()
    mock_task_resp.json.return_value = {"id": "task-12345"}
    mock_client.post.return_value = mock_task_resp

    result = runner.invoke(app, ["task", "submit", "Fix it", "--project", "test-proj"])
    
    assert result.exit_code == 0
    assert "Task submitted: task-123" in result.stdout
    mock_client.post.assert_called_with("/v1/tasks", json={"user_request": "Fix it", "project_id": "proj-1"})

@patch("jarvis.app.terminal._get_api_client")
def test_task_list(mock_client_ctx: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {"id": "t1", "status": "queued", "title": "First"},
        {"id": "t2", "status": "running", "title": "Second"}
    ]
    mock_client.get.return_value = mock_resp

    result = runner.invoke(app, ["task", "list"])
    assert result.exit_code == 0
    assert "First" in result.stdout
    assert "running" in result.stdout

@patch("jarvis.app.terminal._get_api_client")
def test_task_status(mock_client_ctx: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "id": "t1", 
        "title": "Main Task", 
        "status": "running",
        "steps": [
            {"step_index": 0, "status": "completed", "title": "S1"},
            {"step_index": 1, "status": "running", "title": "S2"}
        ]
    }
    mock_client.get.return_value = mock_resp

    # Using full ID
    result = runner.invoke(app, ["task", "status", "t1-1234567890123456789012345678901234567890"])
    assert result.exit_code == 0
    assert "Main Task" in result.stdout
    assert "S2" in result.stdout

@patch("jarvis.app.terminal._get_api_client")
def test_task_approve(mock_client_ctx: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    
    mock_resp = MagicMock()
    mock_client.post.return_value = mock_resp

    result = runner.invoke(app, ["task", "approve", "t1"])
    assert result.exit_code == 0
    assert "Task plan approved: t1" in result.stdout
    mock_client.post.assert_called_with("/v1/tasks/t1/plan/approve")

@patch("jarvis.app.terminal._get_api_client")
def test_task_approve_step(mock_client_ctx: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    
    mock_resp = MagicMock()
    mock_client.post.return_value = mock_resp

    result = runner.invoke(app, ["task", "approve", "t1", "--step", "s1"])
    assert result.exit_code == 0
    assert "Task step approved: s1" in result.stdout
    mock_client.post.assert_called_with("/v1/tasks/t1/steps/s1/approve", json={"reason": None})

@patch("jarvis.app.terminal._get_api_client")
def test_task_deny(mock_client_ctx: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    
    mock_resp = MagicMock()
    mock_client.post.return_value = mock_resp

    result = runner.invoke(app, ["task", "deny", "t1", "--step", "s1", "--reason", "bad tool"])
    assert result.exit_code == 0
    assert "Task step denied: s1" in result.stdout
    mock_client.post.assert_called_with("/v1/tasks/t1/steps/s1/deny", json={"reason": "bad tool"})

@patch("jarvis.app.terminal._get_api_client")
def test_task_resume(mock_client_ctx: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    
    mock_resp = MagicMock()
    mock_client.post.return_value = mock_resp

    result = runner.invoke(app, ["task", "resume", "t1"])
    assert result.exit_code == 0
    assert "Task resumed: t1" in result.stdout
    mock_client.post.assert_called_with("/v1/tasks/t1/resume")

@patch("jarvis.app.terminal._get_api_client")
def test_task_cancel(mock_client_ctx: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    
    mock_resp = MagicMock()
    mock_client.post.return_value = mock_resp

    result = runner.invoke(app, ["task", "cancel", "t1"])
    assert result.exit_code == 0
    assert "Task cancelled: t1" in result.stdout
    mock_client.post.assert_called_with("/v1/tasks/t1/cancel")


@patch("jarvis.app.terminal._get_api_client")
def test_task_show_completed(mock_client_ctx: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    
    task_data = {
        "id": "task-completed-12345678901234567890",
        "title": "Completed Task Title",
        "user_request": "Do work",
        "status": "completed",
        "project_id": "proj-abc",
        "steps": [
            {
                "id": "step-1",
                "step_index": 0,
                "title": "Step One",
                "status": "completed",
                "tool_name": "write_file",
                "output_json": '{"bytes": 42}'
            }
        ]
    }
    
    def mock_get(url, **kwargs):
        resp = MagicMock()
        if url == "/v1/tasks":
            resp.json.return_value = [task_data]
        else:
            resp.json.return_value = task_data
        return resp

    mock_client.get.side_effect = mock_get

    result = runner.invoke(app, ["task", "show", "task-completed"])
    assert result.exit_code == 0
    assert "Completed Task Title" in result.stdout
    assert "completed" in result.stdout
    assert "proj-abc" in result.stdout
    assert "write_file" in result.stdout
    assert "step-1" in result.stdout


@patch("jarvis.app.terminal._get_api_client")
def test_task_show_failed(mock_client_ctx: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    
    task_data = {
        "id": "task-failed-12345678901234567890",
        "title": "Failed Task Title",
        "user_request": "Do illegal work",
        "status": "failed",
        "project_id": None,
        "steps": [
            {
                "id": "step-fail-1",
                "step_index": 0,
                "title": "Write Outside",
                "status": "failed",
                "tool_name": "write_file",
                "error": "Path is outside allowed workspaces: /etc/passwd"
            }
        ]
    }
    
    def mock_get(url, **kwargs):
        resp = MagicMock()
        if url == "/v1/tasks":
            resp.json.return_value = [task_data]
        else:
            resp.json.return_value = task_data
        return resp

    mock_client.get.side_effect = mock_get

    result = runner.invoke(app, ["task", "show", "task-failed"])
    assert result.exit_code == 0
    assert "Failed Task Title" in result.stdout
    assert "failed" in result.stdout
    assert "Error:" in result.stdout
    assert "passwd" in result.stdout


@patch("jarvis.app.terminal._get_api_client")
def test_task_show_waiting_for_approval(mock_client_ctx: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    
    task_data = {
        "id": "task-approval-12345678901234567890",
        "title": "Approval Task Title",
        "user_request": "Delete database",
        "status": "paused",
        "project_id": "proj-xyz",
        "steps": [
            {
                "id": "step-approval-999",
                "step_index": 0,
                "title": "Delete database file",
                "status": "waiting_for_approval",
                "tool_name": "generic_command",
                "output_json": None,
                "error": None
            }
        ]
    }
    
    def mock_get(url, **kwargs):
        resp = MagicMock()
        if url == "/v1/tasks":
            resp.json.return_value = [task_data]
        else:
            resp.json.return_value = task_data
        return resp

    mock_client.get.side_effect = mock_get

    result = runner.invoke(app, ["task", "show", "task-approval"])
    assert result.exit_code == 0
    assert "Approval Task Title" in result.stdout
    assert "step-approval-999" in result.stdout
    assert "Steps Waiting for Approval:" in result.stdout
    assert "uv run jarvis task approve task-app --step step-approval-999" in result.stdout


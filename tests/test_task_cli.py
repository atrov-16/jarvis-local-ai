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

def _task_with_steps(mock_client: MagicMock, statuses: list[str]) -> None:
    detail = {
        "id": "t1",
        "steps": [{"id": f"s{i}", "status": status} for i, status in enumerate(statuses)],
    }

    def get(url: str) -> MagicMock:
        response = MagicMock()
        response.json.return_value = [{"id": "t1"}] if url == "/v1/tasks" else detail
        return response

    mock_client.get.side_effect = get


@patch("jarvis.app.terminal._get_api_client")
def test_task_approve_without_step_approves_the_one_waiting_step(
    mock_client_ctx: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    _task_with_steps(mock_client, ["completed", "waiting_for_approval"])

    result = runner.invoke(app, ["task", "approve", "t1"])

    assert result.exit_code == 0
    assert "Task step approved: s1" in result.stdout
    mock_client.post.assert_called_once_with(
        "/v1/tasks/t1/steps/s1/approve", json={"reason": None}
    )


@patch("jarvis.app.terminal._get_api_client")
def test_task_approve_without_step_fails_when_nothing_is_waiting(
    mock_client_ctx: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    _task_with_steps(mock_client, ["completed"])

    result = runner.invoke(app, ["task", "approve", "t1"])

    assert result.exit_code == 1
    assert "not waiting for approval" in result.stdout
    mock_client.post.assert_not_called()


@patch("jarvis.app.terminal._get_api_client")
def test_task_approve_without_step_refuses_to_guess_between_several(
    mock_client_ctx: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    _task_with_steps(mock_client, ["waiting_for_approval", "waiting_for_approval"])

    result = runner.invoke(app, ["task", "approve", "t1"])

    assert result.exit_code == 1
    assert "--step" in result.stdout
    mock_client.post.assert_not_called()


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


# --- Phase 17 slice 5: the call's arguments are shown before it is approved ------------------

import json  # noqa: E402


def _waiting_task(tool_name: str, arguments: dict, **step_extra) -> dict:
    return {
        "id": "task-args-1234567890123456789012345678",
        "title": "Args Task",
        "user_request": "Do it",
        "status": "paused",
        "project_id": None,
        "final_answer": None,
        "steps": [
            {
                "id": "step-args-1",
                "step_index": 0,
                "title": f"{tool_name}: x",
                "status": "waiting_for_approval",
                "tool_name": tool_name,
                "tool_call_id": "call_1",
                "risk_level": "high",
                "input_json": json.dumps(arguments),
                "output_json": None,
                "error": None,
                **step_extra,
            }
        ],
    }


def _serve(mock_client: MagicMock, task: dict) -> None:
    def mock_get(url, **kwargs):
        resp = MagicMock()
        resp.json.return_value = [task] if url == "/v1/tasks" else task
        return resp

    mock_client.get.side_effect = mock_get


@patch("jarvis.app.terminal._get_api_client")
def test_task_show_prints_the_arguments_and_risk_of_a_waiting_call(
    mock_client_ctx: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    _serve(mock_client, _waiting_task("generic_command", {"binary": "git", "args": ["push"]}))

    result = runner.invoke(app, ["task", "show", "task-args"])

    assert result.exit_code == 0
    assert "binary: git" in result.stdout and "push" in result.stdout
    assert "high" in result.stdout  # the risk level


@patch("jarvis.app.terminal._get_api_client")
def test_task_show_prints_a_diff_for_a_waiting_patch(mock_client_ctx: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    _serve(
        mock_client,
        _waiting_task(
            "patch_file",
            {"path": "a.py", "hunks": [{"search": "old = 1", "replace": "new = 2"}]},
        ),
    )

    result = runner.invoke(app, ["task", "show", "task-args"])

    assert "- old = 1" in result.stdout and "+ new = 2" in result.stdout


@patch("jarvis.app.terminal._get_api_client")
def test_task_show_prints_markup_in_arguments_literally(mock_client_ctx: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    _serve(mock_client, _waiting_task("write_file", {"path": "a", "content": "[red]hi[/red]"}))

    result = runner.invoke(app, ["task", "show", "task-args"])

    assert "[red]hi[/red]" in result.stdout


@patch("jarvis.app.terminal._get_api_client")
def test_task_show_prints_the_final_answer_of_a_completed_task(
    mock_client_ctx: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    task = _waiting_task("echo", {"text": "hi"})
    task.update(status="completed", final_answer="All finished: wrote the file.")
    task["steps"][0].update(status="completed", output_json='{"echo": "hi"}')
    _serve(mock_client, task)

    result = runner.invoke(app, ["task", "show", "task-args"])

    assert "Answer:" in result.stdout and "All finished: wrote the file." in result.stdout


@patch("jarvis.app.terminal._get_api_client")
def test_task_approve_prints_the_arguments_before_it_approves(
    mock_client_ctx: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    _serve(mock_client, _waiting_task("write_file", {"path": "out.txt", "content": "payload"}))
    order: list[str] = []
    mock_client.post.side_effect = lambda *a, **k: order.append("POST") or MagicMock()

    with patch("jarvis.app.terminal.console.print") as printed:
        printed.side_effect = lambda *a, **k: order.append("PRINT:" + " ".join(map(str, a)))
        result = runner.invoke(app, ["task", "approve", "task-args"])

    assert result.exit_code == 0
    post_at = order.index("POST")
    shown_before = " ".join(order[:post_at])
    assert "out.txt" in shown_before and "payload" in shown_before and "high" in shown_before
    mock_client.post.assert_called_once_with(
        "/v1/tasks/task-args-1234567890123456789012345678/steps/step-args-1/approve",
        json={"reason": None},
    )


@patch("jarvis.app.terminal._get_api_client")
def test_task_approve_with_an_explicit_step_also_shows_what_it_approves(
    mock_client_ctx: MagicMock,
) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    _serve(mock_client, _waiting_task("generic_command", {"binary": "rm", "args": ["-rf", "x"]}))

    result = runner.invoke(app, ["task", "approve", "task-args", "--step", "step-args"])

    assert result.exit_code == 0
    assert "binary: rm" in result.stdout and "-rf" in result.stdout
    assert mock_client.post.call_count == 1


@patch("jarvis.app.terminal._get_api_client")
def test_a_step_that_is_not_waiting_is_not_presented_as_being_decided(
    mock_client_ctx: MagicMock,
) -> None:
    # QA finding: "Denying: <args>" was printed for a step that had already completed, right
    # before the server refused the decision.
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    task = _waiting_task("generic_command", {"binary": "rm", "args": ["x"]}, status="completed")
    task["status"] = "completed"
    _serve(mock_client, task)

    deny = runner.invoke(app, ["task", "deny", "task-args", "--step", "step-args"])
    approve = runner.invoke(app, ["task", "approve", "task-args", "--step", "step-args"])

    for result in (deny, approve):
        assert "Denying:" not in result.stdout and "Approving:" not in result.stdout
        assert "binary: rm" not in result.stdout


@patch("jarvis.app.terminal._get_api_client")
def test_task_deny_also_shows_what_it_denies(mock_client_ctx: MagicMock) -> None:
    mock_client = MagicMock()
    mock_client_ctx.return_value.__enter__.return_value = mock_client
    _serve(mock_client, _waiting_task("generic_command", {"binary": "rm", "args": ["x"]}))

    result = runner.invoke(app, ["task", "deny", "task-args", "--step", "step-args"])

    assert result.exit_code == 0 and "binary: rm" in result.stdout


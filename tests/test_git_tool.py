import subprocess

import pytest

from jarvis.tasks.command_runner import CommandRunner
from jarvis.tools.git import GitOperation, GitTool


@pytest.fixture
def runner():
    return CommandRunner()

@pytest.fixture
def git_tool(runner):
    return GitTool(runner)

@pytest.fixture
def repo_path(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    # Init repo
    subprocess.run(["git", "init"], cwd=repo, check=True)
    # Set config for tests
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    return repo

@pytest.mark.asyncio
async def test_git_status_clean(git_tool, repo_path):
    workspaces = [{"path": str(repo_path)}]
    result = await git_tool.execute(operation=GitOperation.STATUS, workspaces=workspaces)
    
    assert result.success
    assert result.data["is_dirty"] is False
    assert "Clean" in result.data["summary_text"]

@pytest.mark.asyncio
async def test_git_status_dirty(git_tool, repo_path):
    # Create untracked file
    (repo_path / "untracked.txt").write_text("hello")
    
    workspaces = [{"path": str(repo_path)}]
    result = await git_tool.execute(operation=GitOperation.STATUS, workspaces=workspaces)
    
    assert result.success
    assert result.data["is_dirty"] is True
    assert result.data["untracked_count"] == 1

@pytest.mark.asyncio
async def test_git_log(git_tool, repo_path):
    # Create a commit
    (repo_path / "file.txt").write_text("v1")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True)
    subprocess.run(["git", "commit", "-m", "first commit"], cwd=repo_path, check=True)
    
    workspaces = [{"path": str(repo_path)}]
    result = await git_tool.execute(operation=GitOperation.LOG, limit=1, workspaces=workspaces)
    
    assert result.success
    assert "first commit" in result.data["log"]

@pytest.mark.asyncio
async def test_git_diff(git_tool, repo_path):
    # Create and commit a file
    file = repo_path / "file.txt"
    file.write_text("v1")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True)
    subprocess.run(["git", "commit", "-m", "v1"], cwd=repo_path, check=True)
    
    # Modify file
    file.write_text("v2")
    
    workspaces = [{"path": str(repo_path)}]
    result = await git_tool.execute(operation=GitOperation.DIFF, workspaces=workspaces)
    
    assert result.success
    assert "+v2" in result.data["diff"]

@pytest.mark.asyncio
async def test_not_a_repo(git_tool, tmp_path):
    not_repo = tmp_path / "not_repo"
    not_repo.mkdir()
    workspaces = [{"path": str(not_repo)}]
    
    # Git status on non-repo
    result = await git_tool.execute(operation=GitOperation.STATUS, workspaces=workspaces)
    assert not result.success
    assert "not part of a Git repository" in result.error

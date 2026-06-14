import pytest
import asyncio
import os
from pathlib import Path
from jarvis.tasks.command_runner import CommandRunner

@pytest.fixture
def runner():
    return CommandRunner()

@pytest.mark.asyncio
async def test_command_runner_success(runner, tmp_path):
    # Use 'echo' as a cross-platform simple command
    # On Windows, 'echo' is a shell builtin, but 'cmd /c echo' works.
    # However, create_subprocess_exec expects a file. 
    # We'll use 'python -c "print(\"hello\")"' as it's guaranteed to be available.
    import sys
    python_exe = sys.executable
    
    result = await runner.run(
        python_exe, 
        ["-c", "print('hello world')"], 
        cwd=tmp_path
    )
    
    assert result.exit_code == 0
    assert "hello world" in result.stdout
    assert not result.timeout_occurred
    assert result.execution_time > 0

@pytest.mark.asyncio
async def test_command_runner_failure(runner, tmp_path):
    import sys
    python_exe = sys.executable
    
    # Run python script that exits with non-zero
    result = await runner.run(
        python_exe, 
        ["-c", "import sys; sys.exit(42)"], 
        cwd=tmp_path
    )
    
    assert result.exit_code == 42
    assert not result.timeout_occurred

@pytest.mark.asyncio
async def test_command_runner_timeout(runner, tmp_path):
    import sys
    python_exe = sys.executable
    
    # Run python script that sleeps longer than timeout
    result = await runner.run(
        python_exe, 
        ["-c", "import time; time.sleep(5)"], 
        cwd=tmp_path,
        timeout=1
    )
    
    assert result.timeout_occurred
    # Exit code might be None or negative depending on how it was killed
    assert result.exit_code != 0

@pytest.mark.asyncio
async def test_command_runner_env_scrubbing(runner, tmp_path):
    import sys
    python_exe = sys.executable
    
    # Set a forbidden key in os.environ
    os.environ["JARVIS_API_TOKEN"] = "secret-token"
    
    result = await runner.run(
        python_exe, 
        ["-c", "import os; print(os.environ.get('JARVIS_API_TOKEN', 'not-found'))"], 
        cwd=tmp_path
    )
    
    assert "not-found" in result.stdout
    assert "secret-token" not in result.stdout

def test_command_result_summary():
    from jarvis.tasks.command_runner import CommandResult
    res = CommandResult(
        exit_code=0,
        stdout="very long output " * 1000,
        stderr="no error",
        execution_time=1.5,
        timeout_occurred=False,
        command="test-cmd",
        working_dir="/tmp"
    )
    
    summary = res.to_summary(max_chars=100)
    assert "Command: test-cmd" in summary
    assert "Status: Success" in summary
    assert len(res.stdout) > 1000
    # Summary should be truncated
    assert "--- Standard Output ---" in summary
    # The summary includes headers, so we check if the actual stdout part is small
    output_part = summary.split("--- Standard Output ---")[1].split("--- Standard Error ---")[0].strip()
    assert len(output_part) <= 100

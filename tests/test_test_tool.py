
import pytest

from jarvis.tasks.command_runner import CommandRunner
from jarvis.tools.test_runner import TestFramework, TestTool


@pytest.fixture
def runner():
    return CommandRunner()

@pytest.fixture
def test_tool(runner):
    return TestTool(runner)

@pytest.mark.asyncio
async def test_detect_pytest(test_tool, tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]")
    assert test_tool._detect_framework(tmp_path) == TestFramework.PYTEST

@pytest.mark.asyncio
async def test_detect_unittest(test_tool, tmp_path):
    (tmp_path / "tests").mkdir()
    assert test_tool._detect_framework(tmp_path) == TestFramework.UNITTEST

@pytest.mark.asyncio
async def test_detect_npm(test_tool, tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert test_tool._detect_framework(tmp_path) == TestFramework.NPM

@pytest.mark.asyncio
async def test_parse_pytest_output(test_tool):
    stdout = """
============================= test session starts ==============================
collected 3 items

test_a.py .                                                              [ 33%]
test_b.py F                                                              [ 66%]
test_c.py s                                                              [100%]

=================================== FAILURES ===================================
____________________________________ test_b ____________________________________
def test_b():
>       assert False
E       assert False
test_b.py:2: AssertionError
=========================== short test summary info ============================
FAILED test_b.py::test_b - assert False
==================== 1 passed, 1 failed, 1 skipped in 0.12s ====================
"""
    data = test_tool._parse_output(TestFramework.PYTEST, stdout, "")
    assert data["passed"] == 1
    assert data["failed"] == 1
    assert data["skipped"] == 1
    assert data["duration"] == "0.12s"
    assert len(data["failures"]) == 1
    assert data["failures"][0]["test_name"] == "test_b"

@pytest.mark.asyncio
async def test_parse_unittest_output(test_tool):
    stdout = """
test_a (tests.test_a.TestA.test_a) ... ok
test_b (tests.test_a.TestA.test_b) ... FAIL
test_c (tests.test_a.TestA.test_c) ... skipped 'reason'

======================================================================
FAIL: test_b (tests.test_a.TestA.test_b)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "C:\\jarvis\\tests\\test_a.py", line 6, in test_b
    self.fail()
AssertionError: None

----------------------------------------------------------------------
Ran 3 tests in 0.001s

FAILED (failures=1, skipped=1)
"""
    data = test_tool._parse_output(TestFramework.UNITTEST, stdout, "")
    assert data["passed"] == 1
    assert data["failed"] == 1
    assert data["skipped"] == 1
    assert data["duration"] == "0.001s"

@pytest.mark.asyncio
async def test_test_tool_execution(test_tool, tmp_path):
    # Create a small pytest project
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "pytest.ini").write_text("[pytest]")
    (workspace / "test_simple.py").write_text("def test_pass(): assert True")
    
    workspaces = [{"path": str(workspace)}]
    result = await test_tool.execute(workspaces=workspaces)
    
    assert result.success
    assert result.data["counts"]["passed"] == 1
    assert "PYTEST" in result.data["summary"]

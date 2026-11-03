"""Run Ruff or mypy and compare the finding count with the committed baseline.

The count is always printed. The exit status is non-zero only if the count is higher than the
baseline, so existing findings are visible and are never reported as a clean result.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

BASELINE_FILE = Path(__file__).resolve().parents[1] / "static-analysis-baseline.json"


def count_ruff() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "--output-format", "json", "."],
        capture_output=True, text=True, check=False,
    )
    return len(json.loads(result.stdout or "[]"))


def count_mypy() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "jarvis"], capture_output=True, text=True, check=False
    )
    match = re.search(r"Found (\d+) errors?", result.stdout)
    if match:
        return int(match.group(1))
    if "Success" in result.stdout:
        return 0
    print(result.stdout, result.stderr)
    raise SystemExit("could not read the mypy result")


def main() -> int:
    tool = sys.argv[1]
    baseline = json.loads(BASELINE_FILE.read_text())[tool]
    count = {"ruff": count_ruff, "mypy": count_mypy}[tool]()
    print(f"{tool}: {count} findings (committed baseline: {baseline}; platform: {sys.platform})")
    if count > baseline:
        print(f"::error::{tool} findings rose from {baseline} to {count}")
        return 1
    if count < baseline:
        print(f"{tool} improved by {baseline - count}: lower the baseline in {BASELINE_FILE.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

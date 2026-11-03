"""Report failed pytest tests from JUnit XML files as GitHub Actions annotations.

Annotations are attached to the check run, so failures are readable from the run page and
through the public check-runs API without downloading logs or artifacts.

Usage: python junit_annotations.py junit-a.xml [junit-b.xml ...]
Always exits 0: it only reports; the test step itself decides pass or fail.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

# GitHub shows at most 10 error annotations per step; the rest are summarised in one.
MAX_ANNOTATIONS = 10
TAIL_CHARS = 1500


def _data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _prop(value: str) -> str:
    return _data(value).replace(":", "%3A").replace(",", "%2C")


def failed_cases(path: str) -> list[tuple[str, str]]:
    """(test id, failure text) for every failed or errored test in one JUnit file."""
    root = ET.parse(path).getroot()
    found: list[tuple[str, str]] = []
    for case in root.iter("testcase"):
        bad = case.find("failure")
        if bad is None:
            bad = case.find("error")
        if bad is None:
            continue
        name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
        text = (bad.get("message") or "") + "\n" + (bad.text or "")[-TAIL_CHARS:]
        found.append((name, text.strip()))
    return found


def main(paths: list[str]) -> int:
    failures: list[tuple[str, str]] = []
    for path in paths:
        try:
            failures.extend(failed_cases(path))
        except (OSError, ET.ParseError) as error:
            print(f"::warning title=JUnit report unreadable::{_data(f'{path}: {error}')}")
    for name, text in failures[:MAX_ANNOTATIONS]:
        print(f"::error title={_prop(name)}::{_data(text)}")
    if len(failures) > MAX_ANNOTATIONS:
        extra = len(failures) - MAX_ANNOTATIONS
        rest = "\n".join(name for name, _ in failures[MAX_ANNOTATIONS:])
        print(f"::error title={_prop(f'{extra} more failed tests')}::{_data(rest)}")
    print(f"{len(failures)} failed test(s) reported", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

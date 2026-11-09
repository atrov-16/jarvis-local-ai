"""The platform markers decide, on every OS, which tests run and which are left for another."""

import pytest
from _platforms import other_platforms, platform_skip_reason

WIN, MAC, LINUX = "win32", "darwin", "linux"


@pytest.mark.parametrize("platform", [WIN, MAC, LINUX])
def test_unmarked_tests_run_everywhere(platform):
    assert platform_skip_reason([], platform) is None
    assert platform_skip_reason(["parametrize", "asyncio"], platform) is None


@pytest.mark.parametrize(
    ("markers", "runs_on"),
    [
        (["windows"], {WIN}),
        (["macos"], {MAC}),
        (["posix"], {MAC, LINUX}),
        (["macos", "posix"], {MAC}),
    ],
)
def test_marked_tests_run_only_on_their_platforms(markers, runs_on):
    for platform in (WIN, MAC, LINUX):
        reason = platform_skip_reason(markers, platform)
        if platform in runs_on:
            assert reason is None, platform
        else:
            assert reason is not None, platform


def test_skip_reason_says_where_the_test_runs():
    assert platform_skip_reason(["macos"], WIN) == "macOS only; run it there (this is Windows)"
    assert platform_skip_reason(["windows"], MAC) == "Windows only; run it there (this is macOS)"
    assert platform_skip_reason(["posix"], WIN) == (
        "Linux/macOS only; run it there (this is Windows)"
    )


@pytest.mark.parametrize("markers", [["windows", "posix"], ["windows", "macos"]])
def test_markers_that_exclude_every_platform_are_an_error(markers):
    # A test that would be skipped on both machines must fail loudly, never skip silently.
    for platform in (WIN, MAC, LINUX):
        with pytest.raises(ValueError, match="never run"):
            platform_skip_reason(markers, platform)


def test_other_platforms_names_what_this_machine_cannot_cover():
    assert other_platforms(WIN) == ["macOS"]
    assert other_platforms(MAC) == ["Windows"]
    assert other_platforms(LINUX) == ["Windows", "macOS"]


@pytest.mark.windows
def test_windows_marker_runs_on_windows():
    import sys

    assert sys.platform == "win32"


@pytest.mark.macos
def test_macos_marker_runs_on_macos():
    import sys

    assert sys.platform == "darwin"


@pytest.mark.posix
def test_posix_marker_runs_off_windows():
    import sys

    assert sys.platform != "win32"

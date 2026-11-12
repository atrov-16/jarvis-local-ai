"""Phase 17, slice 5: what the CLI shows of a tool call before the user approves it.

`describe_call` turns a call's tool name and stored JSON arguments into labelled lines. Nothing is
cut off: the user must see everything they are about to approve. File edits are shown as a diff.
"""

import json

from jarvis.app.call_preview import describe_call


def kinds(lines):
    return [kind for kind, _ in lines]


def texts(lines):
    return [text for _, text in lines]


def test_a_patch_is_shown_as_removed_and_added_lines_per_hunk():
    args = json.dumps(
        {
            "path": "src/app.py",
            "hunks": [
                {"search": "old line 1\nold line 2", "replace": "new line"},
                {"search": "x = 1", "replace": "x = 2\ny = 3"},
            ],
        }
    )

    lines = describe_call("patch_file", args)

    assert texts(lines)[0] == "path: src/app.py"
    assert ("del", "- old line 1") in lines and ("del", "- old line 2") in lines
    assert ("add", "+ new line") in lines
    assert ("add", "+ x = 2") in lines and ("add", "+ y = 3") in lines
    assert sum(1 for t in texts(lines) if t.startswith("@@")) == 2


def test_a_whole_file_write_shows_the_path_and_every_line_of_content():
    content = "\n".join(f"line {i}" for i in range(120))

    lines = describe_call("write_file", json.dumps({"path": "notes.txt", "content": content}))

    assert texts(lines)[0] == "path: notes.txt"
    added = [text for kind, text in lines if kind == "add"]
    assert added == [f"+ line {i}" for i in range(120)]  # not truncated
    assert any("replaces" in text.lower() for kind, text in lines if kind == "meta")


def test_a_delete_shows_the_path_and_whether_it_is_recursive():
    lines = describe_call("delete_file", json.dumps({"path": "build", "recursive": True}))

    assert "path: build" in texts(lines) and "recursive: true" in texts(lines)


def test_a_command_shows_the_binary_and_its_arguments():
    lines = describe_call(
        "generic_command", json.dumps({"binary": "git", "args": ["commit", "-m", "a b"]})
    )

    assert "binary: git" in texts(lines)
    assert any("commit" in t and "-m" in t and "a b" in t for t in texts(lines))


def test_any_other_tool_lists_its_arguments_in_full():
    long_value = "z" * 3000

    lines = describe_call("custom", json.dumps({"alpha": 1, "beta": long_value}))

    assert "alpha: 1" in texts(lines)
    assert f"beta: {long_value}" in texts(lines)


def test_multiline_values_are_shown_line_by_line():
    lines = describe_call("custom", json.dumps({"body": "one\ntwo"}))

    assert texts(lines) == ["body:", "  one", "  two"]


def test_input_that_is_not_json_is_shown_as_it_is():
    assert describe_call("custom", "not json {") == [("meta", "input: not json {")]


def test_a_call_without_input_says_so():
    assert describe_call("custom", None) == [("meta", "(no arguments)")]
    assert describe_call("custom", "{}") == [("meta", "(no arguments)")]


def test_a_patch_with_unexpected_shape_falls_back_to_listing_arguments():
    lines = describe_call("patch_file", json.dumps({"path": "a", "hunks": "oops"}))

    assert "path: a" in texts(lines) and "hunks: oops" in texts(lines)


def test_control_characters_in_arguments_are_shown_not_sent_to_the_terminal() -> None:
    # A model can put escape sequences in a call's arguments (e.g. "erase line, cursor to column
    # 1") so that a terminal shows different text from what is stored and about to be approved.
    # The preview must make them visible instead of emitting them.
    hostile = "rm\x1b[2K\x1b[1G\x07\x7f\u009b"
    calls = [
        ("generic_command", {"args": ["rm", "-rf", "/", hostile]}),
        ("write_file", {"path": f"a{hostile}", "content": f"x\n{hostile}"}),
        ("patch_file", {"path": hostile, "hunks": [{"search": hostile, "replace": hostile}]}),
        ("custom", {hostile: hostile}),
    ]
    for tool, args in calls:
        for _kind, text in describe_call(tool, json.dumps(args)):
            assert all(ch == "\n" or (ch.isprintable()) for ch in text), (tool, text)
    shown = " ".join(t for _k, t in describe_call("custom", json.dumps({"k": "a\x1b[2Kb"})))
    assert shown == "k: a\\x1b[2Kb"

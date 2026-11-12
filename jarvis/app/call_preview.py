"""What the CLI shows of a tool call before the user approves or denies it.

`describe_call` turns a call's tool name and stored JSON arguments into labelled lines
`(kind, text)`; the caller colours them by kind (`add`, `del`, `meta`, `plain`). Nothing is cut
off: the user must see everything they are about to approve. A file edit (`patch_file`) is shown
as a diff, and a whole-file `write_file` as the lines being written. The text is plain: it is
printed literally, never as console markup.
"""

from __future__ import annotations

import json

Line = tuple[str, str]


def describe_call(tool_name: str | None, input_json: str | None) -> list[Line]:
    if not input_json:
        return [("meta", "(no arguments)")]
    try:
        args = json.loads(input_json)
    except json.JSONDecodeError:
        return [("meta", f"input: {input_json}")]
    if not isinstance(args, dict):
        return [("meta", f"input: {input_json}")]
    if not args:
        return [("meta", "(no arguments)")]

    if tool_name == "patch_file":
        lines = _patch_lines(args)
        if lines is not None:
            return lines
    elif tool_name == "write_file" and isinstance(args.get("content"), str):
        return _write_lines(args)
    elif tool_name == "generic_command" and isinstance(args.get("args"), list):
        return _command_lines(args)
    return _generic_lines(args)


def _patch_lines(args: dict[str, object]) -> list[Line] | None:
    hunks = args.get("hunks")
    if not isinstance(hunks, list) or not all(
        isinstance(h, dict)
        and isinstance(h.get("search"), str)
        and isinstance(h.get("replace"), str)
        for h in hunks
    ):
        return None
    lines: list[Line] = [("plain", f"path: {args.get('path')}")]
    for number, hunk in enumerate(hunks, start=1):
        lines.append(("meta", f"@@ change {number} of {len(hunks)} @@"))
        lines.extend(("del", f"- {text}") for text in str(hunk["search"]).split("\n"))
        lines.extend(("add", f"+ {text}") for text in str(hunk["replace"]).split("\n"))
    return lines


def _write_lines(args: dict[str, object]) -> list[Line]:
    body = str(args["content"]).split("\n")
    lines: list[Line] = [
        ("plain", f"path: {args.get('path')}"),
        ("meta", f"writes the whole file ({len(body)} lines); an existing file replaces it"),
    ]
    lines.extend(("add", f"+ {text}") for text in body)
    return lines


def _command_lines(args: dict[str, object]) -> list[Line]:
    raw_args = args["args"]
    words = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
    lines: list[Line] = [
        ("plain", f"binary: {args.get('binary')}"),
        ("plain", "args: " + " ".join(words)),
    ]
    for key, value in args.items():
        if key not in ("binary", "args"):
            lines.extend(_value_lines(key, value))
    return lines


def _generic_lines(args: dict[str, object]) -> list[Line]:
    lines: list[Line] = []
    for key, value in args.items():
        lines.extend(_value_lines(key, value))
    return lines


def _value_lines(key: str, value: object) -> list[Line]:
    text = value if isinstance(value, str) else json.dumps(value)
    if "\n" in text:
        return [("plain", f"{key}:"), *(("plain", f"  {part}") for part in text.split("\n"))]
    return [("plain", f"{key}: {text}")]

"""Loading of the optional `.env` file.

Windows editors and shells routinely write a UTF-8 byte-order mark (Notepad, PowerShell's
`Out-File`), and Windows PowerShell 5.1's `>` writes UTF-16. python-dotenv reads plain
UTF-8 only, so a BOM would turn the first key into `"\\ufeffNAME"` and silently drop it
(often the very line that holds the API token), and UTF-16 would crash startup with a
bare UnicodeDecodeError. This reads UTF-8 with or without a BOM and turns anything else
into an error that says what to do.
"""

from __future__ import annotations

from pathlib import Path


class EnvFileError(ValueError):
    """The .env file exists but cannot be read as UTF-8 text."""


def load_env_file(path: str | Path | None = None) -> bool:
    """Load `.env` (found by python-dotenv's usual search, or at `path`) into os.environ.

    Existing environment variables win over the file. Returns True if a file was loaded;
    False if there was none or python-dotenv is not installed.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    try:
        return bool(load_dotenv(dotenv_path=path, encoding="utf-8-sig"))
    except UnicodeDecodeError as error:
        where = f" ({path})" if path is not None else ""
        raise EnvFileError(
            f"The .env file{where} is not UTF-8 text ({error.reason} at byte {error.start}). "
            "Re-save it as UTF-8; Windows PowerShell 5.1's '>' redirection writes UTF-16, "
            "so create it with an editor or 'Set-Content -Encoding utf8' instead."
        ) from error

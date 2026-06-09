"""Secret redaction helpers."""

from __future__ import annotations

from collections.abc import Iterable

REDACTION = "[REDACTED]"


def redact_text(text: str, secrets: Iterable[str | None]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, REDACTION)
    return redacted


"""Secret redaction helpers."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable

REDACTION = "[REDACTED]"

# Credential shapes redacted even when the value itself is unknown, e.g. a token that arrived
# in a request URL (`/v1/events?token=...`) or an Authorization header echoed in an error.
_CREDENTIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 16+ characters, so ordinary prose ("Basic usage") is left alone.
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}"), rf"\1 {REDACTION}"),
    (
        re.compile(r"(?i)\b(token|access_token|api_key|apikey|key|secret|password)=[^&\s\"'<>]+"),
        rf"\1={REDACTION}",
    ),
    (
        re.compile(r"(?i)(\bx-(?:jarvis-api-token|api-key)['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+"),
        rf"\1{REDACTION}",
    ),
    (re.compile(r"\bsk-or-[A-Za-z0-9_-]{8,}"), REDACTION),
)

# Loggers whose formatter unpacks record.args (uvicorn's AccessFormatter).
_TUPLE_ARGS_LOGGERS = frozenset({"uvicorn.access"})


def redact_text(text: str, secrets: Iterable[str | None]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, REDACTION)
    return redacted


def redact_sensitive(text: str, secrets: Iterable[str | None] = ()) -> str:
    """Redact known secret values, then anything shaped like a credential."""
    # Longest first, so a secret that contains another is not left partially visible.
    known: list[str] = sorted((secret for secret in secrets if secret), key=len, reverse=True)
    redacted = redact_text(text, known)
    for pattern, replacement in _CREDENTIAL_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


class SecretRedactingFilter(logging.Filter):
    """Redacts secrets from log records before any handler formats them.

    Attach it to handlers (a logger-level filter does not see records from child loggers).
    """

    def __init__(self, secrets: Iterable[str | None] = ()) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._secrets: set[str] = set()
        self.add_secrets(secrets)

    def add_secrets(self, secrets: Iterable[str | None]) -> None:
        with self._lock:
            self._secrets.update(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        with self._lock:
            secrets = tuple(self._secrets)
        try:
            self._redact_record(record, secrets)
        except Exception:
            # Never let logging raise into the caller, and never emit what was not redacted.
            record.msg, record.args = "[log record withheld: it could not be redacted]", None
            record.exc_info, record.exc_text, record.stack_info = None, None, None
        return True

    @staticmethod
    def _redact_record(record: logging.LogRecord, secrets: tuple[str, ...]) -> None:
        if isinstance(record.args, tuple) and record.args and record.name in _TUPLE_ARGS_LOGGERS:
            # Their formatter unpacks the arguments, so they stay a tuple, redacted one by one.
            record.args = tuple(_redact_arg(arg, secrets) for arg in record.args)
            record.msg = redact_text(str(record.msg), secrets)
        else:
            # Redact the rendered message: the patterns must see "token=<value>" whole, and
            # must not eat a placeholder such as "token=%s" in the template.
            record.msg = redact_sensitive(record.getMessage(), secrets)
            record.args = None

        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            # Formatters reuse exc_text instead of re-rendering exc_info.
            record.exc_text = redact_sensitive(record.exc_text, secrets)
        if record.stack_info:
            record.stack_info = redact_sensitive(record.stack_info, secrets)


def _redact_arg(value: object, secrets: tuple[str, ...]) -> object:
    if value is None or isinstance(value, int | float):
        return value
    if isinstance(value, str):
        return redact_sensitive(value, secrets)
    # Keep the original object (and its %r / %d formatting) unless it renders a secret.
    for rendered in (str(value), repr(value)):
        if redact_sensitive(rendered, secrets) != rendered:
            return redact_sensitive(str(value), secrets)
    return value


_LOG_FILTER = SecretRedactingFilter()


def get_log_redaction_filter() -> SecretRedactingFilter:
    """The process-wide filter; also used as a `logging.config` filter factory."""
    return _LOG_FILTER


def register_log_secrets(secrets: Iterable[str | None]) -> None:
    """Redact these values from every log record handled by the redaction filter."""
    _LOG_FILTER.add_secrets(secrets)


def install_log_redaction(logger: logging.Logger | None = None) -> None:
    """Attach the redaction filter to every handler of `logger` (default: the root logger)."""
    target = logger or logging.getLogger()
    for handler in target.handlers:
        if _LOG_FILTER not in handler.filters:
            handler.addFilter(_LOG_FILTER)

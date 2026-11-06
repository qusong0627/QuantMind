"""Best-effort redaction for credentials that reach logs or error surfaces."""

from __future__ import annotations

import json
import logging
import os
import re
import traceback
from codecs import getincrementaldecoder
from collections.abc import Mapping
from typing import Any


_SENSITIVE_ENV_MARKERS = (
    "APIKEY",
    "AUTHORIZATION",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "ACCESSKEY",
    "PRIVATEKEY",
    "SIGNINGKEY",
    "CREDENTIAL",
    "COOKIE",
    "SUBSCRIPTIONKEY",
)
_CONNECTION_SECRET_ENV_NAMES = frozenset(
    {
        "DATABASEURL",
        "DBURL",
        "POSTGRESURL",
        "REDISURL",
        "CELERYBROKERURL",
        "CELERYRESULTBACKEND",
    }
)
_HEADER_ENV_NAMES = frozenset({"LLMEXTRAHEADERS", "QMFFACTORNAMESHEADERS"})
_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:[\w.-]*(?:authorization|proxy-authorization|api[_-]?key|"
    r"access[_-]?key(?:[_-]?id)?|access[_-]?token|refresh[_-]?token|"
    r"secret(?:[_-]?id|[_-]?access[_-]?key)?|password|private[_-]?key|"
    r"signing[_-]?key|subscription[_-]?key|credentials?|"
    r"database[_-]?url|db[_-]?url|postgres[_-]?url|redis[_-]?url|"
    r"celery[_-]?(?:broker[_-]?url|result[_-]?backend))|"
    r"authorization|proxy-authorization)"
    r"[\"']?\s*[:=]\s*(?:[A-Za-z][\w-]*\s+)?)"
    r"(['\"]?)[^\s,;\"']+\2"
)
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_URL_PASSWORD = re.compile(r"(?i)(://[^:/\s]+:)[^@/\s]+(@)")
_SK_KEY = re.compile(r"(?i)\bsk-(?:proj-|ant-api\d+-)?[A-Za-z0-9_-]{12,}")
_STANDARD_LOG_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__)


def _replace_assignment(match: re.Match[str]) -> str:
    quote = match.group(2)
    replacement = f"{quote}[REDACTED]{quote}" if quote else "[REDACTED]"
    return f"{match.group(1)}{replacement}"


def configured_secrets(extra_env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    values = []
    source = dict(os.environ)
    if extra_env:
        source.update(extra_env)
    for name, value in source.items():
        normalized_name = re.sub(r"[^A-Z0-9]", "", name.upper())
        if not value:
            continue
        if normalized_name in _CONNECTION_SECRET_ENV_NAMES:
            # Connection URLs can contain credentials in userinfo or query
            # parameters. Redact the whole configured value wherever it appears.
            if len(value) >= 4:
                values.append(value)
        elif any(marker in normalized_name for marker in _SENSITIVE_ENV_MARKERS):
            if len(value) >= 4:
                values.append(value)
        if normalized_name in _HEADER_ENV_NAMES:
            # These variables are JSON maps of outbound HTTP headers. Treat each
            # value as sensitive because custom provider headers can carry keys.
            values.append(value)
            try:
                headers = json.loads(value)
            except (TypeError, ValueError):
                continue
            if isinstance(headers, dict):
                values.extend(
                    str(header_value)
                    for header_value in headers.values()
                    if header_value is not None and len(str(header_value)) >= 4
                )
    return tuple(sorted(set(values), key=len, reverse=True))


def redact_text(value: Any, *, extra_secrets: tuple[str, ...] = ()) -> str:
    """Return text with known env credentials and common credential forms masked."""
    secrets = set(configured_secrets()).union(extra_secrets)
    return _redact_with_secrets(
        str(value), tuple(sorted(secrets, key=len, reverse=True))
    )


def _redact_with_secrets(value: str, secrets: tuple[str, ...]) -> str:
    text = value
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    text = _URL_PASSWORD.sub(r"\1[REDACTED]\2", text)
    text = _BEARER.sub(r"\1[REDACTED]", text)
    text = _SK_KEY.sub("[REDACTED]", text)
    return _ASSIGNMENT.sub(_replace_assignment, text)


def _safe_stream_prefix(text: str, cut: int, secrets: tuple[str, ...]) -> int:
    """Move a chunk boundary backward when a credential match crosses it."""
    if cut <= 0:
        return 0
    safe_cut = cut
    for secret in secrets:
        start = 0
        while True:
            start = text.find(secret, start, cut)
            if start < 0:
                break
            if start + len(secret) > cut:
                safe_cut = min(safe_cut, start)
            start += 1
    for pattern in (_ASSIGNMENT, _BEARER, _URL_PASSWORD, _SK_KEY):
        for match in pattern.finditer(text):
            if match.start() < safe_cut < match.end():
                safe_cut = match.start()
    return safe_cut


def copy_redacted_stream(
    reader: Any, writer: Any, *, extra_env: Mapping[str, str] | None = None
) -> None:
    """Copy a binary subprocess stream while redacting credentials with bounded memory."""
    secrets = configured_secrets(extra_env)
    tail_size = max(4096, max((len(secret) for secret in secrets), default=0) + 256)
    decoder = getincrementaldecoder("utf-8")("replace")
    pending = ""
    hard_limit = 4 * 1024 * 1024

    def emit(value: str) -> None:
        if value:
            writer.write(_redact_with_secrets(value, secrets).encode("utf-8"))

    while True:
        chunk = reader.read(65536)
        if not chunk:
            break
        pending += decoder.decode(chunk)
        if len(pending) > tail_size:
            cut = _safe_stream_prefix(pending, len(pending) - tail_size, secrets)
            if cut:
                emit(pending[:cut])
                pending = pending[cut:]
        if len(pending) > hard_limit:
            emit("[REDACTED: oversized credential-bearing log segment]\n")
            pending = ""

    pending += decoder.decode(b"", final=True)
    emit(pending)
    writer.flush()


def redact_data(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {key: redact_data(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(redact_data(item) for item in value)
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    return value


class SecretRedactionFilter(logging.Filter):
    """Sanitize formatted messages, exception tracebacks, and structured extras."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_text(record.getMessage())
            record.args = ()
        except Exception:
            record.msg = "[log message redaction failed]"
            record.args = ()

        if record.exc_info:
            try:
                record.exc_text = redact_text(
                    "".join(traceback.format_exception(*record.exc_info))
                )
            except Exception:
                record.exc_text = "[exception redaction failed]"
            record.exc_info = None

        for key, value in tuple(record.__dict__.items()):
            if key not in _STANDARD_LOG_RECORD_FIELDS:
                record.__dict__[key] = redact_data(value)
        return True


def install_secret_redaction() -> None:
    """Attach the filter to handlers already installed by the ASGI server."""
    filter_instance = SecretRedactionFilter()
    loggers = [logging.getLogger()]
    loggers.extend(
        item
        for item in logging.root.manager.loggerDict.values()
        if isinstance(item, logging.Logger)
    )
    for logger in loggers:
        for handler in logger.handlers:
            if not any(
                isinstance(existing, SecretRedactionFilter)
                for existing in handler.filters
            ):
                handler.addFilter(filter_instance)

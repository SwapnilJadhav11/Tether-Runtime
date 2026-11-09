"""JSON logs correlated by run, job and trace, with redaction (PD-6, ADR-0015, ADR-0017).

``structlog`` renders JSON through the standard library, so library records (uvicorn, the OTLP
exporter, LangGraph) get the same pipeline. Every line carries the bound context (``run_id``,
``workspace_id``, ``job_id``, ``tool``, ...) and the active ``trace_id``/``span_id``.

Redaction, applied to every record:
- payload, state and credential *keys* are dropped (graph state, job payloads, arguments and
  outputs are never logged);
- registered secret *values* (database password, audit and binding keys) are replaced wherever
  they appear, including exception text;
- bearer tokens, JWTs and Tether API keys are replaced by shape.
Tracebacks are rendered as text without local variables.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from typing import IO, Any

import structlog
from opentelemetry import trace

_DROPPED_KEYS = frozenset(
    {
        "payload",
        "arguments",
        "args",
        "task_input",
        "history",
        "state",
        "output",
        "outcome_payload",
        "messages",
        "body",
        "headers",
        "authorization",
        "api_key",
        "x-api-key",
        "token",
        "password",
        "secret",
    }
)
REDACTED = "[REDACTED]"
_PATTERNS = (
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"), f"Bearer {REDACTED}"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"), REDACTED),
    (re.compile(r"tak_[a-z0-9-]{1,63}_[A-Z2-7]{16,}"), REDACTED),
)
# Secret values (not names) to scrub; filled at startup from settings and the key ring.
_SECRETS: set[str] = set()
_MIN_SECRET_LENGTH = 8
# Libraries whose INFO lines carry request URLs, which can contain tool arguments.
_QUIET = ("httpx", "httpcore")


def register_secret(value: str | None) -> None:
    """Scrub ``value`` from every future log line."""
    if value and len(value) >= _MIN_SECRET_LENGTH:
        _SECRETS.add(value)


def scrub(text: str) -> str:
    for secret in sorted(_SECRETS, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, REDACTED)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {key: _scrub_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_scrub_value(item) for item in value]
    return value


def _drop_sensitive_keys(
    logger: Any, method: str, event: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for name in [name for name in event if name.lower() in _DROPPED_KEYS]:
        del event[name]
    return event


def _add_trace_context(
    logger: Any, method: str, event: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    try:
        context = trace.get_current_span().get_span_context()
    except Exception:
        return event
    if context.is_valid:
        event.setdefault("trace_id", f"{context.trace_id:032x}")
        event.setdefault("span_id", f"{context.span_id:016x}")
    return event


def _scrub_event(
    logger: Any, method: str, event: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return {name: _scrub_value(value) for name, value in event.items()}


_SHARED: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    _add_trace_context,
    _drop_sensitive_keys,
]


def configure_logging(level: str = "INFO", *, stream: IO[str] | None = None) -> logging.Handler:
    """Route all logging through the JSON pipeline on ``stream`` (stdout by default).

    Replaces a handler installed by an earlier call; returns the new one.
    """
    structlog.configure(
        processors=[*_SHARED, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,  # plain traceback text: no locals
            _scrub_event,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(formatter)
    handler.set_name("tether-json")
    root = logging.getLogger()
    for existing in [h for h in root.handlers if h.get_name() == "tether-json"]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
    for name in _QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler


def remove_handler(handler: logging.Handler) -> None:
    logging.getLogger().removeHandler(handler)


def get_logger(name: str) -> Any:
    return structlog.stdlib.get_logger(name)


@contextmanager
def bound(**context: Any) -> Iterator[None]:
    """Bind ``context`` (ids only, never payloads) to every log line inside the block."""
    values = {name: str(value) for name, value in context.items() if value is not None}
    with structlog.contextvars.bound_contextvars(**values):
        yield

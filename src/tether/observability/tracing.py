"""Trace context and spans (architecture AD-13, ADR-0015: traces are best-effort).

A run's trace context is created at submission and persisted as ``runs.trace_parent``; every job
row carries the context that enqueued it (``jobs.trace_parent``). A job's span is a child of the
run's stored context and links to the enqueuing span, so every segment of a run shares one trace
id across processes and restarts. Postgres stays the record: nothing here is read back to decide
anything, and a telemetry failure never reaches the caller.

Spans carry identifiers and bounded descriptors only, never payloads, arguments, outputs, tokens
or keys (ADR-0017). A failing span records the exception *type*, never its message.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping
from types import TracebackType
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import (
    INVALID_SPAN,
    Link,
    NonRecordingSpan,
    Span,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
    Tracer,
)

_TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})")
_MAX_ATTRIBUTE_LENGTH = 128


def tracer() -> Tracer:
    return trace.get_tracer("tether")


def format_traceparent(context: SpanContext) -> str:
    """W3C ``traceparent`` (version 00) for ``context``."""
    return f"00-{context.trace_id:032x}-{context.span_id:016x}-{int(context.trace_flags):02x}"


def parse_traceparent(value: object) -> SpanContext | None:
    """The remote span context in a stored ``traceparent``; ``None`` if absent or malformed."""
    if not isinstance(value, str):
        return None
    match = _TRACEPARENT.fullmatch(value)
    if match is None:
        return None
    trace_id, span_id = int(match.group(1), 16), int(match.group(2), 16)
    if not trace_id or not span_id:
        return None
    return SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=True,
        trace_flags=TraceFlags(int(match.group(3), 16)),
    )


def _current_context() -> SpanContext | None:
    try:
        context = trace.get_current_span().get_span_context()
    except Exception:
        return None
    return context if context.is_valid else None


def current_traceparent() -> str | None:
    """The active span's ``traceparent``, to persist with a run or a job; ``None`` if none."""
    context = _current_context()
    return None if context is None else format_traceparent(context)


def current_trace_id() -> str | None:
    """The active trace id (32 hex digits), stored on audit events for correlation."""
    context = _current_context()
    return None if context is None else f"{context.trace_id:032x}"


def _attribute(value: Any) -> Any:
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str):
        return value[:_MAX_ATTRIBUTE_LENGTH]
    return None


def _clean(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    cleaned = {}
    for name, value in (attributes or {}).items():
        converted = _attribute(value)
        if converted is not None:
            cleaned[name] = converted
    return cleaned


class span:  # noqa: N801 - used like a function: ``with tracing.span(...) as current:``
    """A span that is current inside the block; never raises on its own account.

    ``parent``: a stored ``traceparent`` to continue (default: the current context).
    ``links``: stored ``traceparent`` values to link to (malformed or missing ones are skipped).
    """

    def __init__(
        self,
        name: str,
        *,
        parent: str | None = None,
        links: Iterable[str | None] = (),
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        self._name = name
        self._parent = parent
        self._links = links
        self._kind = kind
        self._attributes = attributes
        self._manager: Any = None
        self._span: Span = INVALID_SPAN

    def __enter__(self) -> Span:
        try:
            context = None
            remote = parse_traceparent(self._parent)
            if remote is not None:
                context = trace.set_span_in_context(NonRecordingSpan(remote))
            # A link to the parent itself adds nothing (and some viewers then hide the parent).
            parent_span_id = remote.span_id if remote is not None else None
            links = [
                Link(linked)
                for value in self._links
                if (linked := parse_traceparent(value)) and linked.span_id != parent_span_id
            ]
            self._manager = tracer().start_as_current_span(
                self._name,
                context=context,
                kind=self._kind,
                links=links,
                attributes=_clean(self._attributes),
                record_exception=False,
                set_status_on_exception=False,
            )
            self._span = self._manager.__enter__()
        except Exception:
            self._manager, self._span = None, INVALID_SPAN
        return self._span

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._manager is None:
            return
        try:
            if exc_type is not None:
                mark_error(self._span, exc_type.__name__)
            # The exception (if any) keeps propagating from here; the span only records its type.
            self._manager.__exit__(None, None, None)
        except Exception:
            return


def annotate(target: Span, **attributes: Any) -> None:
    """Set attributes on ``target``; never raises."""
    try:
        target.set_attributes(_clean(attributes))
    except Exception:
        return


def annotate_current(**attributes: Any) -> None:
    """Set attributes on the current span (e.g. the request span); never raises."""
    try:
        annotate(trace.get_current_span(), **attributes)
    except Exception:
        return


def event(name: str, **attributes: Any) -> None:
    """Add an event to the current span; never raises."""
    try:
        trace.get_current_span().add_event(name, _clean(attributes))
    except Exception:
        return


def mark_error(target: Span, reason: str) -> None:
    """Mark ``target`` failed with a short, non-sensitive reason (a type name or a category)."""
    try:
        target.set_status(Status(StatusCode.ERROR, reason[:_MAX_ATTRIBUTE_LENGTH]))
    except Exception:
        return

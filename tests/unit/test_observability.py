"""S1.9 / S7.3: trace context helpers, JSON logs with redaction, metric helpers, and telemetry
that can never break execution (ADR-0015 "telemetry is best-effort", ADR-0017 scrubber).
"""

from __future__ import annotations

import io
import logging
import os
import time
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic import SecretStr

from tether.observability import logging as tether_logging
from tether.observability import metrics, telemetry, tracing
from tether.settings import Settings

Delta = Callable[[], dict[Any, float]]

# --- trace context --------------------------------------------------------------------------


def test_traceparent_round_trips(spans: InMemorySpanExporter) -> None:
    with tracing.span("probe") as span:
        traceparent = tracing.current_traceparent()
    context = span.get_span_context()
    flags = int(context.trace_flags)
    assert flags & 0x01  # sampled
    assert traceparent == f"00-{context.trace_id:032x}-{context.span_id:016x}-{flags:02x}"
    parsed = tracing.parse_traceparent(traceparent)
    assert parsed is not None
    assert (parsed.trace_id, parsed.span_id) == (context.trace_id, context.span_id)
    assert tracing.format_traceparent(parsed) == traceparent


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "garbage",
        "00-" + "0" * 32 + "-" + "1" * 16 + "-01",  # all-zero trace id
        "00-" + "a" * 32 + "-" + "0" * 16 + "-01",  # all-zero span id
        "ff-" + "a" * 32 + "-" + "b" * 16 + "-01",  # invalid version
        "00-" + "A" * 32 + "-" + "b" * 16 + "-01",  # upper case is not W3C
        "00-" + "a" * 32 + "-" + "b" * 16 + "-01-extra",
        123,
    ],
)
def test_a_malformed_traceparent_is_ignored(value: Any) -> None:
    assert tracing.parse_traceparent(value) is None


def test_there_is_no_traceparent_outside_a_span() -> None:
    assert tracing.current_traceparent() is None
    assert tracing.current_trace_id() is None


def test_a_span_parented_on_a_stored_traceparent_joins_that_trace_and_links_another(
    spans: InMemorySpanExporter,
) -> None:
    with tracing.span("root"):
        root = tracing.current_traceparent()
    with tracing.span("enqueuer"):
        enqueuer = tracing.current_traceparent()
    with tracing.span("segment", parent=root, links=[enqueuer, None, "garbage"]):
        pass
    by_name = {s.name: s for s in spans.get_finished_spans()}
    segment, root_span = by_name["segment"], by_name["root"]
    assert segment.context is not None
    assert root_span.context is not None
    assert segment.context.trace_id == root_span.context.trace_id
    assert segment.parent is not None
    assert segment.parent.span_id == root_span.context.span_id
    enqueuer_span = by_name["enqueuer"]
    assert enqueuer_span.context is not None
    assert [link.context.span_id for link in segment.links] == [enqueuer_span.context.span_id]


def test_a_link_to_the_parent_itself_is_not_recorded(spans: InMemorySpanExporter) -> None:
    with tracing.span("root"):
        root = tracing.current_traceparent()
    with tracing.span("segment", parent=root, links=[root]):
        pass
    [segment] = [s for s in spans.get_finished_spans() if s.name == "segment"]
    assert segment.parent is not None
    assert list(segment.links) == []


def test_a_failing_span_records_only_the_exception_type(spans: InMemorySpanExporter) -> None:
    secret = f"sensitive-{uuid.uuid4().hex}"
    with pytest.raises(ValueError, match="sensitive"), tracing.span("boom"):
        raise ValueError(secret)
    [span] = spans.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "ValueError"
    assert secret not in span.to_json()


def test_tracing_failures_never_break_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> Any:
        raise RuntimeError("tracer unavailable")

    monkeypatch.setattr(tracing, "tracer", broken)
    ran = False
    with tracing.span("anything", parent="garbage") as span:
        tracing.annotate(span, **{"tether.probe": 1})
        tracing.event("probe")
        ran = True
    assert ran
    with pytest.raises(KeyError), tracing.span("still raises the caller's own error"):
        raise KeyError("x")


# --- JSON logs ------------------------------------------------------------------------------


def test_bound_ids_and_the_trace_id_appear_on_log_lines(
    log_lines: Callable[[], list[dict[str, Any]]],
) -> None:
    log = tether_logging.get_logger("probe")
    with (
        tracing.span("op") as span,
        tether_logging.bound(run_id="run-1", workspace_id="ws-1", job_id="job-1", tool="probe"),
    ):
        log.info("hello")
    [line] = [line for line in log_lines() if line["event"] == "hello"]
    assert line["run_id"] == "run-1"
    assert line["workspace_id"] == "ws-1"
    assert line["job_id"] == "job-1"
    assert line["tool"] == "probe"
    assert line["trace_id"] == f"{span.get_span_context().trace_id:032x}"
    assert line["level"] == "info"
    assert "timestamp" in line


def test_standard_library_records_get_the_same_json_and_context(
    log_lines: Callable[[], list[dict[str, Any]]],
) -> None:
    with tether_logging.bound(run_id="run-2"):
        logging.getLogger("some.library").warning("plain %s", "message")
    [line] = [line for line in log_lines() if line["event"] == "plain message"]
    assert line["run_id"] == "run-2"
    assert line["level"] == "warning"


def test_a_registered_secret_is_scrubbed_from_messages_fields_and_tracebacks(
    raw_log: io.StringIO,
) -> None:
    secret = f"s3cr3t-{uuid.uuid4().hex}"
    tether_logging.register_secret(secret)
    log = tether_logging.get_logger("probe")
    log.info(f"connecting with {secret}", detail={"nested": [secret]}, note=secret)
    try:
        raise RuntimeError(f"failed with {secret}")
    except RuntimeError:
        log.exception("boom")
    logging.getLogger("some.library").error("library saw %s", secret)
    text = raw_log.getvalue()
    assert secret not in text
    assert "[REDACTED]" in text


def test_payload_state_and_credential_keys_are_dropped(
    log_lines: Callable[[], list[dict[str, Any]]],
) -> None:
    log = tether_logging.get_logger("probe")
    dropped = {
        "payload": {"a": 1},
        "arguments": {"service": "x"},
        "task_input": {"message": "m"},
        "state": {"history": []},
        "history": [],
        "output": {"x": 1},
        "authorization": "Bearer abc",
        "headers": {"x-api-key": "k"},
        "api_key": "k",
        "token": "t",
    }
    log.info("with payloads", kept="yes", **dropped)
    [line] = [line for line in log_lines() if line["event"] == "with payloads"]
    assert line["kept"] == "yes"
    assert not set(dropped) & set(line)


def test_tokens_and_api_keys_are_redacted_by_shape(raw_log: io.StringIO) -> None:
    jwt_like = "eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiJhbGljZSJ9.c2lnbmF0dXJlc2lnbmF0dXJl"
    api_key = "tak_app-a_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567ABCDEFGHIJKLMNOPQRST"
    tether_logging.get_logger("probe").info(f"got {jwt_like} and {api_key}")
    logging.getLogger("some.library").warning("Authorization: Bearer %s", jwt_like)
    text = raw_log.getvalue()
    assert jwt_like not in text
    assert api_key not in text
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" not in text


def test_exception_locals_are_never_rendered(raw_log: io.StringIO) -> None:
    local_value = f"LOCAL-{uuid.uuid4().hex}"

    def fail() -> None:
        hidden = local_value
        raise ValueError(f"no locals please ({len(hidden)})")

    try:
        fail()
    except ValueError:
        tether_logging.get_logger("probe").exception("failed")
    text = raw_log.getvalue()
    assert "no locals please" in text
    assert local_value not in text


def test_noisy_http_client_request_lines_are_not_logged(raw_log: io.StringIO) -> None:
    # httpx logs every request URL at INFO; a URL can carry tool arguments (ADR-0017).
    logging.getLogger("httpx").info("HTTP Request: GET http://target/services/secret-arg")
    assert "secret-arg" not in raw_log.getvalue()


# --- metrics --------------------------------------------------------------------------------


def _key(name: str, **attributes: str) -> tuple[str, frozenset[tuple[str, Any]]]:
    return name, frozenset(attributes.items())


def test_metric_helpers_record_with_bounded_labels(metric_values: Delta) -> None:
    metrics.policy_decision("allow", "get_service_health")
    metrics.tool_executed("get_service_health", "completed", 0.01)
    changed = metric_values()
    assert changed[_key("tether.policy.decisions", decision="allow", tool="get_service_health")]
    assert changed[_key("tether.tool.duration", tool="get_service_health", outcome="completed")]


def test_the_queue_depth_gauge_reports_the_latest_snapshot(metric_values: Delta) -> None:
    metrics.set_queue_depth({("advance_run", "due"): 3, ("execute_tool", "leased"): 1})
    metrics.set_queue_depth({("advance_run", "due"): 2})
    changed = metric_values()
    assert changed.get(_key("tether.jobs.queued", kind="advance_run", state="due")) == 2
    assert _key("tether.jobs.queued", kind="execute_tool", state="leased") not in changed


def test_metric_failures_never_break_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken(dict[str, Any]):
        def __getitem__(self, key: str) -> Any:
            raise RuntimeError("meter unavailable")

    monkeypatch.setattr(metrics, "_INSTRUMENTS", Broken())
    metrics.run_started()
    metrics.run_finished("succeeded", 1.0)
    metrics.job_processed("advance_run", "completed", 0.1)
    metrics.model_call("scripted", "succeeded", 0.1)


# --- process setup --------------------------------------------------------------------------


def test_an_unreachable_collector_neither_blocks_nor_raises() -> None:
    tracer_provider, meter_provider = telemetry.build_providers(
        service="tether-test",
        env="test",
        endpoint="http://127.0.0.1:9",
        export_timeout_seconds=0.5,
        metric_interval_seconds=0.2,
    )
    started = time.monotonic()
    with tracer_provider.get_tracer("probe").start_as_current_span("probe"):
        meter_provider.get_meter("probe").create_counter("probe").add(1)
    tracer_provider.force_flush(2000)
    meter_provider.force_flush(2000)
    tracer_provider.shutdown()
    meter_provider.shutdown(timeout_millis=2000)
    assert time.monotonic() - started < 10


def test_setup_turns_off_third_party_agent_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    telemetry.disable_third_party_tracing()
    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"


def test_the_database_password_is_registered_with_the_scrubber(raw_log: io.StringIO) -> None:
    password = f"pw-{uuid.uuid4().hex}"
    settings = Settings(
        database_url=SecretStr(f"postgresql://tether_app:{password}@db:5432/tether"),
    )
    telemetry.register_settings_secrets(settings)
    tether_logging.get_logger("probe").warning(f"could not reach {password}")
    assert password not in raw_log.getvalue()

"""T-16 / W10.2: one governed run with ``Sensitive`` values, scanned across every sink.

The run submits ``rotate_secret`` (high risk, approval-gated), whose argument ``api_token`` and
output ``new_token`` are ``Sensitive``. Two canaries mark them. Afterwards the scan looks in:
captured spans, captured log lines, every table of the ``tether`` schema, the LangGraph
checkpoint tables, every response of every ``/v1`` route for that run under each audience's
credentials, and every history and tool manifest handed to the model provider.

Where a canary may appear is the exceptions ADR-0017 §2 writes down, and nothing else:

* the **output** canary (``new_token``) may appear nowhere;
* the **argument** canary (``api_token``) may appear only in the checkpoint (graph state), the
  execute job's payload, and the *assistant* message in the model's own history (the model wrote
  that call). It may not appear in a tool, user, system or manifest entry, a log line, a span, an
  audit payload, an approval row, the ledger, or any response.

Not covered, because it does not exist yet: verification evidence (W5.1, architecture §12.3).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tests.integration.evidence_support import (
    BOOT_SENSITIVE,
    CANARY_ARG,
    CANARY_OUT,
    RecordingProvider,
    alice,
    approval_id,
    approve,
    bob,
    call,
    drain,
    ops_admin,
    rows,
    run_status,
    settings_for,
    submit,
)
from tests.integration.evidence_support import (
    demo_target as demo_target,
)
from tests.integration.evidence_support import (
    provider as provider,
)
from tests.integration.evidence_support import (
    runtime as runtime,
)
from tests.integration.sink_scan import (
    KNOWN_WRITES,
    RouteSinks,
    fill,
    inventory,
    scan,
    table_sinks,
)

from tether.observability import tracing
from tether.observability.logging import get_logger
from tether.registry.masking import MASK
from tether.runtime.driver import WorkerRuntime

CANARIES = {"arg": CANARY_ARG, "out": CANARY_OUT}
# ADR-0017 §2: "Unmasked arguments exist only in the model-produced history (the checkpoint) and
# in the job payload needed to execute the call." The model's own assistant message is the same
# history. Nothing else is exempt, and the output canary has no exemption at all.
EXEMPT = {"arg": ["table:tether.jobs", "checkpoint:langgraph.", "provider:assistant:"]}


# --- the scanner can fail (negative self-tests) ----------------------------------------------


def test_t16_the_scanner_reports_a_canary_planted_in_each_kind_of_sink() -> None:
    clean = {
        "span:a": "{}",
        "log": "job processed",
        "table:tether.runs": "{}",
        "checkpoint:langgraph.checkpoints": "{}",
        "route:GET /v1/x": "200",
        "provider:history:0": "[]",
    }
    assert scan(clean, CANARIES) == []
    for sink in clean:
        planted = {**clean, sink: f"before {CANARY_OUT} after"}
        assert scan(planted, CANARIES) == [f"out (raw) in {sink}"], sink


@pytest.mark.parametrize("form", ["hex", "base64", "base64url"])
def test_t16_the_scanner_sees_encoded_forms_of_a_canary(form: str) -> None:
    from tests.integration.sink_scan import forms

    planted = {"table:tether.ledger_entries": f'{{"x": "{forms(CANARY_OUT)[form]}"}}'}
    # base64 and base64url coincide when the encoding has no "+" or "/"; either way it is found.
    assert f"out ({form}) in table:tether.ledger_entries" in scan(planted, CANARIES)


def test_t16_an_exemption_covers_only_the_named_sinks_and_only_its_canary() -> None:
    sinks = {
        "table:tether.jobs": f"{CANARY_ARG} {CANARY_OUT}",
        "table:tether.approvals": CANARY_ARG,
    }
    assert scan(sinks, CANARIES, EXEMPT) == [
        "out (raw) in table:tether.jobs",
        "arg (raw) in table:tether.approvals",
    ]


# --- the governed run --------------------------------------------------------------------------


class Scene:
    """What the governed run produced, for the assertions of both tests below."""

    def __init__(self) -> None:
        self.run_id = uuid.uuid4()
        self.approval_id = uuid.uuid4()
        self.ledger_id = uuid.uuid4()
        self.routes = RouteSinks()
        self.get_templates: list[str] = []


async def sensitive_run(runtime: WorkerRuntime, seeded: Any, tmp_path: Path) -> Scene:
    scene = Scene()
    scene.run_id = await submit(runtime, seeded, tmp_path, "rotate")
    assert run_status(seeded, scene.run_id) == "awaiting_approval"
    scene.approval_id = approval_id(seeded, scene.run_id)
    # Pending: what the requester and the approver can read before the action runs.
    pending = {
        "requester run": ("GET", f"/v1/tasks/runs/{scene.run_id}", alice()),
        "approver list": ("GET", "/v1/approvals/approvals", bob()),
        "approver detail": ("GET", f"/v1/approvals/approvals/{scene.approval_id}", bob()),
        "approver run view": ("GET", f"/v1/approvals/runs/{scene.run_id}", bob()),
    }
    for label, (method, url, creds) in pending.items():
        response = await call(seeded, tmp_path, method, url, headers=creds)
        assert response.status_code == 200, (label, response.text)
        scene.routes.record(f"pending {label}", response)
    decided = await approve(seeded, tmp_path, scene.approval_id)
    assert decided.status_code == 200, decided.text
    scene.routes.record("approve", decided)
    await drain(runtime)
    assert run_status(seeded, scene.run_id) == "succeeded"
    [(ledger_id,)] = rows(
        seeded, "SELECT id FROM tether.ledger_entries WHERE run_id = %s", scene.run_id
    )
    scene.ledger_id = ledger_id
    return scene


async def sweep_routes(scene: Scene, seeded: Any, tmp_path: Path) -> None:
    """Call every route of the app with each audience's credentials, for this run."""
    found = inventory(settings_for(seeded.db, tmp_path))
    # A route this scan cannot drive is a hole in the evidence: fail loudly, never skip it.
    assert found.unknown == [], f"routes the T-16 scan does not know how to call: {found.unknown}"
    values = {
        "run_id": scene.run_id,
        "approval_id": scene.approval_id,
        "ledger_id": scene.ledger_id,
    }
    callers = {"requester": alice(), "approver": bob(), "ops": ops_admin()}
    for template in found.gets:
        url = fill(template, values)
        assert url is not None, f"no value for a path parameter of GET {template}"
        scene.get_templates.append(template)
        for who, creds in callers.items():
            response = await call(seeded, tmp_path, "GET", url, headers=creds)
            scene.routes.record(f"GET {template} as {who}", response)
    writes = [
        ("POST", f"/v1/tasks/runs/{scene.run_id}/cancel", alice(), None),
        ("POST", f"/v1/ops/runs/{scene.run_id}/cancel", ops_admin(), None),
        (
            "POST",
            f"/v1/ops/ledger/{scene.ledger_id}/reconcile",
            ops_admin(),
            {"outcome": "applied", "justification": "evidence"},
        ),
        (
            "POST",
            f"/v1/approvals/{scene.approval_id}/reject",
            bob(),
            {"reason": "too late"},
        ),
    ]
    assert {path for _, path, _, _ in writes}  # every known write is driven below
    assert len(writes) == len(KNOWN_WRITES) - 1  # all but task submission (done in the run)
    for method, url, creds, payload in writes:
        response = await call(seeded, tmp_path, method, url, headers=creds, json=payload)
        scene.routes.record(f"{method} {url.replace(str(scene.run_id), '{run_id}')}", response)


def span_sinks(spans: InMemorySpanExporter) -> dict[str, str]:
    finished = spans.get_finished_spans()
    assert finished
    return {f"span:{i}:{span.name}": span.to_json() for i, span in enumerate(finished)}


def provider_sinks(provider: RecordingProvider) -> dict[str, str]:
    sinks: dict[str, str] = {}
    for turn, history in enumerate(provider.histories):
        assistant = [m for m in history if m["role"] == "assistant"]
        others = [m for m in history if m["role"] != "assistant"]
        sinks[f"provider:assistant:{turn}"] = json.dumps(assistant, sort_keys=True)
        sinks[f"provider:history:{turn}"] = json.dumps(others, sort_keys=True)
    for turn, manifest in enumerate(provider.manifests):
        sinks[f"provider:manifest:{turn}"] = json.dumps(manifest, sort_keys=True)
    return sinks


def all_sinks(
    scene: Scene,
    seeded: Any,
    spans: InMemorySpanExporter,
    log_text: str,
    provider: RecordingProvider,
) -> dict[str, str]:
    return {
        **span_sinks(spans),
        "log": log_text,
        **table_sinks(seeded.db.migrator_dsn),
        **scene.routes.sinks,
        **provider_sinks(provider),
    }


@pytest.mark.asyncio
async def test_t16_a_sensitive_value_reaches_no_sink_outside_the_adr_0017_exceptions(
    runtime: WorkerRuntime,
    seeded: Any,
    tmp_path: Path,
    provider: RecordingProvider,
    spans: InMemorySpanExporter,
    raw_log: Any,
) -> None:
    scene = await sensitive_run(runtime, seeded, tmp_path)
    await sweep_routes(scene, seeded, tmp_path)
    sinks = all_sinks(scene, seeded, spans, raw_log.getvalue(), provider)

    # The scan looked at something in every kind of sink (it is not vacuous).
    kinds = {name.split(":", 1)[0] for name in sinks}
    assert kinds == {"span", "log", "table", "checkpoint", "route", "provider"}
    assert "job processed" in sinks["log"]
    checkpoints = [text for name, text in sinks.items() if name.startswith("checkpoint:")]
    assert any(len(text) > 100 for text in checkpoints)
    assert any(name.startswith("route:GET /v1/tasks/runs/{run_id} as requester") for name in sinks)
    assert scene.get_templates  # the GET routes were discovered, not hard-coded
    # What the model was given is the masked result and never the raw output.
    [tool_message] = [
        m for m in provider.histories[-1] if m["role"] == "tool" and m["outcome"] == "applied"
    ]
    assert MASK in tool_message["content"]
    assert "rotated" in tool_message["content"]

    # 1. The output canary appears nowhere. 2. The argument canary appears only where ADR-0017
    #    §2 says unmasked arguments live.
    assert scan(sinks, CANARIES, EXEMPT) == []
    # Positive control through the real collectors: without the exemptions the argument canary
    # *is* found, and exactly in the sinks ADR-0017 names (never in a tool or user message).
    unexempted = scan(sinks, {"arg": CANARY_ARG})
    assert unexempted, "the argument canary was never seen: the scan would pass for any reason"
    assert all(
        finding.split(" in ", 1)[1].startswith(tuple(EXEMPT["arg"])) for finding in unexempted
    ), unexempted
    assert any(" in provider:assistant:" in finding for finding in unexempted)
    assert any(" in table:tether.jobs" in finding for finding in unexempted)
    assert any(" in checkpoint:langgraph." in finding for finding in unexempted)


@pytest.mark.asyncio
async def test_t16_a_canary_planted_in_a_real_sink_of_the_run_is_found(
    runtime: WorkerRuntime,
    seeded: Any,
    tmp_path: Path,
    provider: RecordingProvider,
    spans: InMemorySpanExporter,
    raw_log: Any,
) -> None:
    """The scan on the real collectors can fail: plant the output canary, see it reported."""
    scene = await sensitive_run(runtime, seeded, tmp_path)
    baseline = all_sinks(scene, seeded, spans, raw_log.getvalue(), provider)
    assert scan(baseline, CANARIES, EXEMPT) == []

    # a log line, through the production logging pipeline
    get_logger("tests.t16").info("planted", detail=CANARY_OUT)
    planted = all_sinks(scene, seeded, spans, raw_log.getvalue(), provider)
    assert scan(planted, CANARIES, EXEMPT) == ["out (raw) in log"]

    # a span attribute
    with tracing.span("t16.planted", attributes={"tether.detail": CANARY_OUT}):
        pass
    found = scan(all_sinks(scene, seeded, spans, "", provider), CANARIES, EXEMPT)
    assert len(found) == 1
    assert found[0].startswith("out (raw) in span:")

    # a table column of the run's own row (a column any API response or report could show)
    rows(
        seeded,
        "UPDATE tether.runs SET outcome_summary = %s WHERE id = %s RETURNING 1",
        CANARY_OUT,
        scene.run_id,
    )
    found = scan({**baseline, **table_sinks(seeded.db.migrator_dsn)}, CANARIES, EXEMPT)
    assert found == ["out (raw) in table:tether.runs"]

    # an exempt sink does not excuse the other canary: the output canary in the job payload
    rows(
        seeded,
        "UPDATE tether.jobs SET payload = payload || jsonb_build_object('x', %s::text)"
        " WHERE run_id = %s RETURNING 1",
        CANARY_OUT,
        scene.run_id,
    )
    found = scan(table_sinks(seeded.db.migrator_dsn), CANARIES, EXEMPT)
    assert "out (raw) in table:tether.jobs" in found

    # a route response and a provider message (the same machinery, fed one hostile response)
    scene.routes.record("planted", httpx.Response(200, text=CANARY_OUT))
    assert scan(scene.routes.sinks, CANARIES, EXEMPT) == ["out (raw) in route:planted"]
    provider.histories.append([{"role": "tool", "content": CANARY_ARG, "outcome": "applied"}])
    assert scan(provider_sinks(provider), CANARIES, EXEMPT) == [
        f"arg (raw) in provider:history:{len(provider.histories) - 1}"
    ]
    assert BOOT_SENSITIVE  # the same bootstrap the run used

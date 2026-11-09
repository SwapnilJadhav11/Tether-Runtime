"""Runtime metrics, exported over OTLP to the collector (architecture AD-13: no Prometheus).

Labels are bounded sets only: run statuses, job kinds and outcomes, policy decisions, registered
tool names, provider names, reconciliation outcomes. Never run ids, user ids, workspace ids or
arguments. Callers record state changes from ``UnitOfWork.on_commit`` so a rolled-back
transaction counts nothing. Recording never raises.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from typing import Any

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Histogram, Observation

_METER = metrics.get_meter("tether")
# Seconds, from a few milliseconds (a policy decision) to the default run deadline.
_SECONDS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300, 900, 3600]
_MAX_LABEL_LENGTH = 64


def _histogram(name: str, description: str) -> Histogram:
    return _METER.create_histogram(
        name, unit="s", description=description, explicit_bucket_boundaries_advisory=_SECONDS
    )


_INSTRUMENTS: dict[str, Any] = {
    "tether.runs.started": _METER.create_counter(
        "tether.runs.started", unit="{run}", description="Runs submitted (TX1 committed)"
    ),
    "tether.runs.finished": _METER.create_counter(
        "tether.runs.finished", unit="{run}", description="Runs reaching a terminal status"
    ),
    "tether.run.duration": _histogram(
        "tether.run.duration", "Submission to terminal status, end to end"
    ),
    "tether.job.pickup_latency": _histogram(
        "tether.job.pickup_latency", "From a job being due to a worker leasing it"
    ),
    "tether.job.duration": _histogram("tether.job.duration", "Processing time of one delivery"),
    "tether.jobs.dead_lettered": _METER.create_counter(
        "tether.jobs.dead_lettered", unit="{job}", description="Jobs ended by TX12"
    ),
    "tether.tool.duration": _histogram("tether.tool.duration", "One tool send, by outcome"),
    "tether.tool.retries": _METER.create_counter(
        "tether.tool.retries", unit="{retry}", description="Tool sends scheduled again"
    ),
    "tether.ledger.indeterminate": _METER.create_counter(
        "tether.ledger.indeterminate",
        unit="{entry}",
        description="Writes whose outcome became indeterminate",
    ),
    "tether.reconciliations": _METER.create_counter(
        "tether.reconciliations", unit="{entry}", description="Indeterminate entries resolved"
    ),
    "tether.policy.decisions": _METER.create_counter(
        "tether.policy.decisions", unit="{decision}", description="Recorded policy decisions"
    ),
    "tether.approval.wait_duration": _histogram(
        "tether.approval.wait_duration", "Approval requested to decided, expired or closed"
    ),
    "tether.model.call.duration": _histogram(
        "tether.model.call.duration", "One model provider call, by outcome"
    ),
}

_DEPTH_LOCK = threading.Lock()
_DEPTH: dict[tuple[str, str], int] = {}


def _observe_depth(options: CallbackOptions) -> Iterable[Observation]:
    with _DEPTH_LOCK:
        snapshot = dict(_DEPTH)
    return [
        Observation(count, {"kind": kind, "state": state})
        for (kind, state), count in snapshot.items()
    ]


_INSTRUMENTS["tether.jobs.queued"] = _METER.create_observable_gauge(
    "tether.jobs.queued",
    callbacks=[_observe_depth],
    unit="{job}",
    description="Jobs by kind and state: due, scheduled (future run_after) or leased",
)


def _label(value: str) -> str:
    return value[:_MAX_LABEL_LENGTH]


def _emit(name: str, value: float, **labels: str) -> None:
    try:
        instrument = _INSTRUMENTS[name]
        attributes = {key: _label(label) for key, label in labels.items()}
        if isinstance(instrument, Histogram):
            instrument.record(value, attributes)
        else:
            instrument.add(value, attributes)
    except Exception:
        return


def run_started() -> None:
    _emit("tether.runs.started", 1)


def run_finished(status: str, duration_seconds: float | None) -> None:
    _emit("tether.runs.finished", 1, status=status)
    if duration_seconds is not None:
        _emit("tether.run.duration", duration_seconds, status=status)


def job_picked_up(kind: str, waited_seconds: float) -> None:
    _emit("tether.job.pickup_latency", max(waited_seconds, 0.0), kind=kind)


def job_processed(kind: str, outcome: str, duration_seconds: float) -> None:
    _emit("tether.job.duration", duration_seconds, kind=kind, outcome=outcome)


def job_dead_lettered(kind: str) -> None:
    _emit("tether.jobs.dead_lettered", 1, kind=kind)


def tool_executed(tool: str, outcome: str, duration_seconds: float) -> None:
    _emit("tether.tool.duration", duration_seconds, tool=tool, outcome=outcome)


def tool_retry(tool: str) -> None:
    _emit("tether.tool.retries", 1, tool=tool)


def ledger_indeterminate(tool: str) -> None:
    _emit("tether.ledger.indeterminate", 1, tool=tool)


def reconciled(outcome: str) -> None:
    _emit("tether.reconciliations", 1, outcome=outcome)


def policy_decision(decision: str, tool: str) -> None:
    """``tool`` must be a registered tool name (or a fixed placeholder), never model text."""
    _emit("tether.policy.decisions", 1, decision=decision, tool=tool)


def approval_resolved(status: str, waited_seconds: float | None) -> None:
    if waited_seconds is not None:
        _emit("tether.approval.wait_duration", max(waited_seconds, 0.0), status=status)


def model_call(provider: str, outcome: str, duration_seconds: float) -> None:
    _emit("tether.model.call.duration", duration_seconds, provider=provider, outcome=outcome)


def set_queue_depth(snapshot: Mapping[tuple[str, str], int]) -> None:
    """Replace the queue-depth snapshot the gauge reports at its next collection."""
    try:
        with _DEPTH_LOCK:
            _DEPTH.clear()
            _DEPTH.update(snapshot)
    except Exception:
        return

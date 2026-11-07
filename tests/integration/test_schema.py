"""S1.5: tether schema v1 — tables, terminal-status guard, constraints, workspace-aware FKs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg import errors
from psycopg.types.json import Jsonb

from tether.core.statuses import (
    TERMINAL_RUN_STATUSES,
    ApprovalStatus,
    JobKind,
    JobState,
    LedgerResolution,
    LedgerState,
    RunStatus,
)

WS_A, WS_B = "ws-a", "ws-b"
EXPECTED_TABLES = {
    "workspaces",
    "issuers",
    "api_keys",
    "agents",
    "api_key_agents",
    "policy_versions",
    "runs",
    "approvals",
    "ledger_entries",
    "audit_heads",
    "audit_events",
    "jobs",
    "model_calls",
    "access_events",
}
RUN_CHILD_TABLES = (
    "approvals",
    "ledger_entries",
    "audit_heads",
    "audit_events",
    "jobs",
    "model_calls",
)


def owner(db: Any) -> psycopg.Connection[Any]:
    return psycopg.connect(db.migrator_dsn, autocommit=True)


def app(db: Any) -> psycopg.Connection[Any]:
    return psycopg.connect(db.app_dsn, autocommit=True)


# --- Shape -------------------------------------------------------------------------------------


def test_core_schema_tables_exist(fresh_db: Any) -> None:
    with owner(fresh_db) as conn:
        found = conn.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = 'tether' AND table_type = 'BASE TABLE'"
        ).fetchall()
    assert {row[0] for row in found} - {"alembic_version"} == EXPECTED_TABLES


def test_migration_head_is_0002(fresh_db: Any) -> None:
    with owner(fresh_db) as conn:
        versions = conn.execute("SELECT version_num FROM tether.alembic_version").fetchall()
    assert versions == [("0002",)]


def test_database_time_defaults_use_clock_timestamp(fresh_db: Any) -> None:
    with owner(fresh_db) as conn:
        defaults = conn.execute(
            "SELECT table_name, column_name, column_default FROM information_schema.columns"
            " WHERE table_schema = 'tether' AND column_default IS NOT NULL"
            " AND data_type LIKE 'timestamp%'"
        ).fetchall()
    assert defaults, "expected timestamp columns with database defaults"
    for table, column, default in defaults:
        assert "clock_timestamp()" in default, f"{table}.{column} defaults to {default}"


# --- Terminal-status guard (invariant 8, ADR-0006) ---------------------------------------------


@pytest.mark.parametrize("terminal", sorted(TERMINAL_RUN_STATUSES))
def test_terminal_status_never_changes(seeded: Any, rows: Any, terminal: str) -> None:
    run_id = uuid.uuid4()
    with owner(seeded.db) as conn:
        rows.insert(conn, "runs", rows.run(WS_A, run_id=run_id, status=terminal))
    with app(seeded.db) as conn:
        for target in RunStatus:
            if target == terminal:
                continue
            with pytest.raises(errors.CheckViolation):
                conn.execute("UPDATE tether.runs SET status = %s WHERE id = %s", (target, run_id))


@pytest.mark.parametrize("terminal", sorted(TERMINAL_RUN_STATUSES))
def test_terminal_runs_still_accept_other_column_updates(
    seeded: Any, rows: Any, terminal: str
) -> None:
    run_id = uuid.uuid4()
    with owner(seeded.db) as conn:
        rows.insert(conn, "runs", rows.run(WS_A, run_id=run_id, status=terminal))
    with app(seeded.db) as conn:
        conn.execute(
            "UPDATE tether.runs SET outcome_summary = 'note', cancel_requested_at = %s,"
            " cancel_requested_by = 'user-1', status = status WHERE id = %s",
            (datetime.now(UTC), run_id),
        )


def test_the_guard_applies_to_the_owning_role_too(seeded: Any, rows: Any) -> None:
    run_id = uuid.uuid4()
    with owner(seeded.db) as conn:
        rows.insert(conn, "runs", rows.run(WS_A, run_id=run_id, status="succeeded"))
        with pytest.raises(errors.CheckViolation):
            conn.execute("UPDATE tether.runs SET status = 'running' WHERE id = %s", (run_id,))


def test_non_terminal_transitions_are_allowed(seeded: Any) -> None:
    with app(seeded.db) as conn:
        for status in ("running", "awaiting_tool", "running", "succeeded"):
            conn.execute("UPDATE tether.runs SET status = %s WHERE id = %s", (status, seeded.run_a))
        with pytest.raises(errors.CheckViolation):
            conn.execute("UPDATE tether.runs SET status = 'failed' WHERE id = %s", (seeded.run_a,))


# --- Status CHECK constraints match core.statuses (PD-18) --------------------------------------


def test_every_run_status_is_accepted_and_unknown_is_rejected(seeded: Any, rows: Any) -> None:
    with owner(seeded.db) as conn:
        for status in RunStatus:
            rows.insert(conn, "runs", rows.run(WS_A, status=status))
        with pytest.raises(errors.CheckViolation):
            rows.insert(conn, "runs", rows.run(WS_A, status="paused"))


@pytest.mark.parametrize(
    ("table", "column", "values"),
    [
        ("approvals", "status", list(ApprovalStatus)),
        ("ledger_entries", "state", list(LedgerState)),
        ("ledger_entries", "resolution", list(LedgerResolution)),
        ("jobs", "state", list(JobState)),
        ("jobs", "kind", list(JobKind)),
    ],
)
def test_child_status_columns_accept_exactly_the_core_values(
    seeded: Any, rows: Any, table: str, column: str, values: list[str]
) -> None:
    with owner(seeded.db) as conn:
        for index, value in enumerate(values):
            overrides: dict[str, Any] = {column: value}
            if table in ("approvals", "ledger_entries"):
                overrides["step_ordinal"] = index
            if table == "ledger_entries":
                overrides["idempotency_key"] = "tth_" + "abcdefgh"[index] * 52
            if table == "approvals" and value in ("approved", "rejected"):
                overrides |= {"decided_by": "user-2", "decided_at": datetime.now(UTC)}
            if column == "resolution" and value.startswith("reconciled_"):
                overrides |= {"resolved_by": "user-3", "justification": "checked the target"}
            if column == "state" and value == "leased":
                overrides |= {"lease_token": uuid.uuid4(), "lease_expires_at": datetime.now(UTC)}
            rows.insert(conn, table, rows.child(table, WS_A, seeded.run_a, **overrides))
        bad: dict[str, Any] = {column: "bogus"}
        if table in ("approvals", "ledger_entries"):
            bad |= {"step_ordinal": 99, "idempotency_key": "tth_" + "z" * 52}
            if table == "approvals":
                bad.pop("idempotency_key")
        with pytest.raises(errors.CheckViolation):
            rows.insert(conn, table, rows.child(table, WS_A, seeded.run_a, **bad))


@pytest.mark.parametrize(
    ("table", "overrides"),
    [
        ("approvals", {"call_hash": "A" * 64}),
        ("approvals", {"step_ordinal": -1}),
        ("approvals", {"status": "approved"}),  # decided approvals need a decider
        ("ledger_entries", {"idempotency_key": "tth_not-a-key"}),
        ("ledger_entries", {"effect": "delete"}),
        ("ledger_entries", {"attempts": -1}),
        ("audit_events", {"mac": "short"}),
        ("jobs", {"max_attempts": 0}),
        ("model_calls", {"provider_role": "tertiary"}),
        ("model_calls", {"attempt": 0}),
        # F6: coupled-column consistency.
        ("ledger_entries", {"resolution": "reconciled_applied"}),
        ("ledger_entries", {"resolution": "reconciled_abandon", "resolved_by": "user-3"}),
        ("ledger_entries", {"resolution": "reconciled_not_applied", "justification": "why"}),
        ("jobs", {"state": "leased"}),
        ("jobs", {"state": "leased", "lease_token": uuid.uuid4()}),
        ("approvals", {"decided_at": datetime(2026, 1, 1, tzinfo=UTC)}),
        ("approvals", {"decided_by": "user-2"}),
        ("model_calls", {"finished_at": datetime(2000, 1, 1, tzinfo=UTC)}),
    ],
)
def test_row_level_checks(seeded: Any, rows: Any, table: str, overrides: dict[str, Any]) -> None:
    with owner(seeded.db) as conn, pytest.raises(errors.CheckViolation):
        rows.insert(conn, table, rows.child(table, WS_A, seeded.run_a, **overrides))


def test_cancel_request_fields_go_together(seeded: Any) -> None:
    with app(seeded.db) as conn, pytest.raises(errors.CheckViolation):
        conn.execute(
            "UPDATE tether.runs SET cancel_requested_at = %s WHERE id = %s",
            (datetime.now(UTC), seeded.run_a),
        )


# --- Uniqueness ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table", "second"),
    [
        ("approvals", {"id": uuid.uuid4()}),
        ("ledger_entries", {"id": uuid.uuid4(), "idempotency_key": "tth_" + "b" * 52}),
        ("ledger_entries", {"id": uuid.uuid4(), "step_ordinal": 1}),
        ("audit_events", {"dedupe_key": "dedupe-2"}),
        ("audit_events", {"seq": 2}),
        ("audit_heads", {}),
        ("model_calls", {"id": uuid.uuid4()}),
    ],
)
def test_unique_constraints(seeded: Any, rows: Any, table: str, second: dict[str, Any]) -> None:
    row = rows.child(table, WS_A, seeded.run_a)
    with owner(seeded.db) as conn:
        rows.insert(conn, table, row)
        with pytest.raises(errors.UniqueViolation):
            rows.insert(conn, table, {**row, **second})


def test_one_active_policy_version_per_workspace(seeded: Any, rows: Any) -> None:
    def version(workspace: str, name: str, *, active: bool) -> dict[str, Any]:
        return {"workspace_id": workspace, "version": name, "active": active}

    with owner(seeded.db) as conn:
        rows.insert(conn, "policy_versions", version(WS_A, "v1", active=True))
        rows.insert(conn, "policy_versions", version(WS_A, "v0", active=False))
        rows.insert(conn, "policy_versions", version(WS_B, "v1", active=True))
        with pytest.raises(errors.UniqueViolation):
            rows.insert(conn, "policy_versions", version(WS_A, "v2", active=True))


def test_issuer_iss_is_unique_per_workspace_and_algorithm_is_pinned(seeded: Any, rows: Any) -> None:
    issuer = {
        "workspace_id": WS_A,
        "issuer_id": "tasks",
        "iss": "https://issuer.example/tasks",
        "audiences": ["tether-tasks"],
        "algorithm": "EdDSA",
        "public_keys": Jsonb({"kid-1": "public"}),
    }
    with owner(seeded.db) as conn:
        rows.insert(conn, "issuers", issuer)
        with pytest.raises(errors.UniqueViolation):
            rows.insert(conn, "issuers", {**issuer, "issuer_id": "other"})
        with pytest.raises(errors.CheckViolation):
            rows.insert(
                conn, "issuers", {**issuer, "issuer_id": "x", "iss": "i2", "algorithm": "none"}
            )
        with pytest.raises(errors.CheckViolation):
            rows.insert(
                conn, "issuers", {**issuer, "issuer_id": "y", "iss": "i3", "audiences": ["admin"]}
            )


def test_api_key_hash_shape_is_enforced(seeded: Any, rows: Any) -> None:
    key = {"key_id": "KA", "workspace_id": WS_A, "salt": bytes(16), "secret_hash": bytes(32)}
    with owner(seeded.db) as conn:
        rows.insert(conn, "api_keys", key)
        with pytest.raises(errors.CheckViolation):
            rows.insert(conn, "api_keys", {**key, "key_id": "KB", "salt": bytes(15)})


# --- Workspace-aware foreign keys (owner decision 2) ------------------------------------------


@pytest.mark.parametrize("table", RUN_CHILD_TABLES)
def test_children_cannot_reference_a_run_in_another_workspace(
    seeded: Any, rows: Any, table: str
) -> None:
    with owner(seeded.db) as conn:
        # run_a belongs to ws-a: claiming it from ws-b must fail on the composite FK ...
        with pytest.raises(errors.ForeignKeyViolation):
            rows.insert(conn, table, rows.child(table, WS_B, seeded.run_a))
        # ... while the same row in the run's own workspace is valid.
        rows.insert(conn, table, rows.child(table, WS_A, seeded.run_a))


def test_run_agent_must_belong_to_the_runs_workspace(seeded: Any, rows: Any) -> None:
    with owner(seeded.db) as conn, pytest.raises(errors.ForeignKeyViolation):
        rows.insert(conn, "runs", rows.run(WS_B, agent="only-a"))


def test_api_key_agents_stay_within_one_workspace(seeded: Any, rows: Any) -> None:
    with owner(seeded.db) as conn:
        rows.insert(conn, "agents", {"workspace_id": WS_B, "agent_id": "only-b"})
        rows.insert(
            conn,
            "api_keys",
            {"key_id": "KA", "workspace_id": WS_A, "salt": bytes(16), "secret_hash": bytes(32)},
        )
        rows.insert(
            conn, "api_key_agents", {"workspace_id": WS_A, "key_id": "KA", "agent_id": "only-a"}
        )
        with pytest.raises(errors.ForeignKeyViolation):
            rows.insert(
                conn, "api_key_agents", {"workspace_id": WS_A, "key_id": "KA", "agent_id": "only-b"}
            )
        with pytest.raises(errors.ForeignKeyViolation):
            rows.insert(
                conn, "api_key_agents", {"workspace_id": WS_B, "key_id": "KA", "agent_id": "only-b"}
            )

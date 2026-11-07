"""SQLAlchemy Core table definitions for the ``tether`` schema (PD-3); Alembic's target_metadata.

These mirror migration ``0002`` column for column, including constraint and index names, so
``alembic revision --autogenerate`` reports no differences. CHECK constraints, the
terminal-status trigger and the role grants live in the migrations only: autogenerate does not
compare them. LangGraph's ``langgraph`` schema is never part of this metadata (PD-4).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

TETHER_SCHEMA = "tether"

metadata = MetaData(schema=TETHER_SCHEMA)

_CLOCK = text("clock_timestamp()")


def _created_at() -> Column[Any]:
    return Column("created_at", TIMESTAMP(timezone=True), nullable=False, server_default=_CLOCK)


def _run_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["workspace_id", "run_id"],
        ["tether.runs.workspace_id", "tether.runs.id"],
        name=f"fk_{table}_run",
    )


def _workspace_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["workspace_id"], ["tether.workspaces.id"], name=f"fk_{table}_workspace"
    )


# --- Bootstrap-owned (written by `tether bootstrap apply`; read-only for the app role) -------

workspaces = Table(
    "workspaces",
    metadata,
    Column("id", Text, nullable=False),
    _created_at(),
    PrimaryKeyConstraint("id", name="pk_workspaces"),
)

issuers = Table(
    "issuers",
    metadata,
    Column("workspace_id", Text, nullable=False),
    Column("issuer_id", Text, nullable=False),
    Column("iss", Text, nullable=False),
    Column("audiences", ARRAY(Text), nullable=False),
    Column("algorithm", Text, nullable=False),
    Column("public_keys", JSONB, nullable=False),
    _created_at(),
    PrimaryKeyConstraint("workspace_id", "issuer_id", name="pk_issuers"),
    UniqueConstraint("workspace_id", "iss", name="uq_issuers_workspace_id_iss"),
    _workspace_fk("issuers"),
)

api_keys = Table(
    "api_keys",
    metadata,
    Column("key_id", Text, nullable=False),
    Column("workspace_id", Text, nullable=False),
    Column("salt", LargeBinary, nullable=False),
    Column("secret_hash", LargeBinary, nullable=False),
    _created_at(),
    PrimaryKeyConstraint("key_id", name="pk_api_keys"),
    UniqueConstraint("workspace_id", "key_id", name="uq_api_keys_workspace_id_key_id"),
    _workspace_fk("api_keys"),
)

agents = Table(
    "agents",
    metadata,
    Column("workspace_id", Text, nullable=False),
    Column("agent_id", Text, nullable=False),
    _created_at(),
    PrimaryKeyConstraint("workspace_id", "agent_id", name="pk_agents"),
    _workspace_fk("agents"),
)

api_key_agents = Table(
    "api_key_agents",
    metadata,
    Column("workspace_id", Text, nullable=False),
    Column("key_id", Text, nullable=False),
    Column("agent_id", Text, nullable=False),
    PrimaryKeyConstraint("key_id", "agent_id", name="pk_api_key_agents"),
    ForeignKeyConstraint(
        ["workspace_id", "key_id"],
        ["tether.api_keys.workspace_id", "tether.api_keys.key_id"],
        name="fk_api_key_agents_api_key",
    ),
    ForeignKeyConstraint(
        ["workspace_id", "agent_id"],
        ["tether.agents.workspace_id", "tether.agents.agent_id"],
        name="fk_api_key_agents_agent",
    ),
)

policy_versions = Table(
    "policy_versions",
    metadata,
    Column("workspace_id", Text, nullable=False),
    Column("version", Text, nullable=False),
    Column("active", Boolean, nullable=False, server_default=text("false")),
    _created_at(),
    PrimaryKeyConstraint("workspace_id", "version", name="pk_policy_versions"),
    _workspace_fk("policy_versions"),
    Index(
        "uq_policy_versions_one_active",
        "workspace_id",
        unique=True,
        postgresql_where=text("active"),
    ),
)

# --- Runtime -------------------------------------------------------------------------------

runs = Table(
    "runs",
    metadata,
    Column("id", UUID(as_uuid=True), nullable=False),
    Column("workspace_id", Text, nullable=False),
    Column("agent_id", Text, nullable=False),
    Column("requester_snapshot", JSONB, nullable=False),
    Column("task_input", JSONB, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'queued'")),
    Column("outcome_summary", Text),
    Column("cancel_requested_at", TIMESTAMP(timezone=True)),
    Column("cancel_requested_by", Text),
    Column("deadline_at", TIMESTAMP(timezone=True), nullable=False),
    Column("deadline_exceeded_at", TIMESTAMP(timezone=True)),
    Column("driver_generation", BigInteger, nullable=False, server_default=text("0")),
    Column("binding_key_id", Text),
    Column("trace_parent", Text),
    _created_at(),
    PrimaryKeyConstraint("id", name="pk_runs"),
    UniqueConstraint("workspace_id", "id", name="uq_runs_workspace_id_id"),
    ForeignKeyConstraint(
        ["workspace_id", "agent_id"],
        ["tether.agents.workspace_id", "tether.agents.agent_id"],
        name="fk_runs_agent",
    ),
    Index("ix_runs_workspace_id_created_at", "workspace_id", "created_at"),
)

approvals = Table(
    "approvals",
    metadata,
    Column("id", UUID(as_uuid=True), nullable=False),
    Column("workspace_id", Text, nullable=False),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("step_ordinal", BigInteger, nullable=False),
    Column("call_hash", Text, nullable=False),
    Column("tool", Text, nullable=False),
    Column("tool_version", Text, nullable=False),
    Column("arguments_masked", JSONB, nullable=False),
    Column("preconditions_masked", JSONB, nullable=False),
    Column("rationale", Text),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("expires_at", TIMESTAMP(timezone=True), nullable=False),
    Column("decided_by", Text),
    Column("decided_at", TIMESTAMP(timezone=True)),
    Column("decision_reason", Text),
    Column("policy_version", Text),
    _created_at(),
    PrimaryKeyConstraint("id", name="pk_approvals"),
    UniqueConstraint("run_id", "step_ordinal", name="uq_approvals_run_id_step_ordinal"),
    _run_fk("approvals"),
)

ledger_entries = Table(
    "ledger_entries",
    metadata,
    Column("id", UUID(as_uuid=True), nullable=False),
    Column("workspace_id", Text, nullable=False),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("step_ordinal", BigInteger, nullable=False),
    Column("tool", Text, nullable=False),
    Column("tool_version", Text, nullable=False),
    Column("effect", Text, nullable=False),
    Column("idempotency_support", Text, nullable=False),
    Column("call_hash", Text, nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("state", Text, nullable=False, server_default=text("'pending'")),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("outcome_payload_masked", JSONB),
    Column("outcome_reason", Text),
    Column("resolution", Text),
    Column("resolved_by", Text),
    Column("justification", Text),
    _created_at(),
    PrimaryKeyConstraint("id", name="pk_ledger_entries"),
    UniqueConstraint("run_id", "step_ordinal", name="uq_ledger_entries_run_id_step_ordinal"),
    UniqueConstraint("idempotency_key", name="uq_ledger_entries_idempotency_key"),
    _run_fk("ledger_entries"),
)

audit_heads = Table(
    "audit_heads",
    metadata,
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("workspace_id", Text, nullable=False),
    Column("seq", BigInteger, nullable=False, server_default=text("0")),
    Column("last_mac", Text),
    Column("key_id", Text, nullable=False),
    PrimaryKeyConstraint("run_id", name="pk_audit_heads"),
    _run_fk("audit_heads"),
)

audit_events = Table(
    "audit_events",
    metadata,
    Column("workspace_id", Text, nullable=False),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("seq", BigInteger, nullable=False),
    Column("dedupe_key", Text, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("actor", Text),
    Column("tool", Text),
    Column("payload_masked", JSONB, nullable=False),
    Column("policy_version", Text),
    Column("trace_id", Text),
    _created_at(),
    Column("prev_mac", Text),
    Column("mac", Text, nullable=False),
    Column("key_id", Text, nullable=False),
    PrimaryKeyConstraint("run_id", "seq", name="pk_audit_events"),
    UniqueConstraint("run_id", "dedupe_key", name="uq_audit_events_run_id_dedupe_key"),
    _run_fk("audit_events"),
    Index("ix_audit_events_workspace_id_actor", "workspace_id", "actor"),
    Index("ix_audit_events_workspace_id_tool", "workspace_id", "tool"),
    Index("ix_audit_events_workspace_id_created_at", "workspace_id", "created_at"),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", UUID(as_uuid=True), nullable=False),
    Column("kind", Text, nullable=False),
    Column("workspace_id", Text, nullable=False),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("ref_id", UUID(as_uuid=True)),
    Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("run_after", TIMESTAMP(timezone=True), nullable=False, server_default=_CLOCK),
    Column("state", Text, nullable=False, server_default=text("'ready'")),
    Column("lease_token", UUID(as_uuid=True)),
    Column("lease_expires_at", TIMESTAMP(timezone=True)),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("max_attempts", Integer, nullable=False),
    Column("concurrency_key", Text),
    Column("trace_parent", Text),
    _created_at(),
    PrimaryKeyConstraint("id", name="pk_jobs"),
    _run_fk("jobs"),
    Index("ix_jobs_state_run_after", "state", "run_after"),
)

model_calls = Table(
    "model_calls",
    metadata,
    Column("id", UUID(as_uuid=True), nullable=False),
    Column("workspace_id", Text, nullable=False),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("turn_ordinal", Integer, nullable=False),
    Column("provider_role", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("occurrence_id", UUID(as_uuid=True), nullable=False),
    Column("status", Text, nullable=False),
    Column("input_tokens", Integer),
    Column("output_tokens", Integer),
    Column("cost_usd", Numeric(12, 6)),
    Column("error_code", Text),
    Column("started_at", TIMESTAMP(timezone=True), nullable=False),
    Column("finished_at", TIMESTAMP(timezone=True)),
    Column("trace_id", Text),
    PrimaryKeyConstraint("id", name="pk_model_calls"),
    UniqueConstraint("occurrence_id", name="uq_model_calls_occurrence_id"),
    _run_fk("model_calls"),
    Index("ix_model_calls_run_id", "run_id"),
)

access_events = Table(
    "access_events",
    metadata,
    Column("id", UUID(as_uuid=True), nullable=False),
    Column("workspace_id", Text, nullable=False),
    Column("actor", Text, nullable=False),
    Column("audience", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("target_run_id", UUID(as_uuid=True)),
    Column("details", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    _created_at(),
    PrimaryKeyConstraint("id", name="pk_access_events"),
    _workspace_fk("access_events"),
    Index("ix_access_events_workspace_id_created_at", "workspace_id", "created_at"),
)


# --- Alembic context options shared by env.py and the autogenerate test --------------------


def include_name(name: str | None, type_: str, parent_names: Mapping[str, str | None]) -> bool:
    """Only the ``tether`` schema is Alembic's; ``langgraph`` belongs to the saver (PD-4)."""
    if type_ == "schema":
        return name == TETHER_SCHEMA
    return True


MIGRATION_CONTEXT_OPTIONS: dict[str, Any] = {
    "include_schemas": True,
    "include_name": include_name,
    "version_table_schema": TETHER_SCHEMA,
}

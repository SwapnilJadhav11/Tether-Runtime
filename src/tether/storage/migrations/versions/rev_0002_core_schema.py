"""Core schema v1: authoritative Tether tables, terminal-status guard, role grants (S1.5).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HEX64 = "'^[0-9a-f]{64}$'"
_AUDIENCES = "'tether-tasks', 'tether-approvals', 'tether-ops'"
_TERMINAL = "'succeeded', 'failed', 'cancelled', 'timed_out', 'abandoned'"

BOOTSTRAP_TABLES = (
    "workspaces",
    "issuers",
    "api_keys",
    "agents",
    "api_key_agents",
    "policy_versions",
)
RUNTIME_TABLES = (
    "runs",
    "approvals",
    "ledger_entries",
    "audit_heads",
    "audit_events",
    "jobs",
    "model_calls",
    "access_events",
)

UPGRADE = [
    # --- Bootstrap-owned -----------------------------------------------------------------
    """
    CREATE TABLE tether.workspaces (
        id text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        CONSTRAINT pk_workspaces PRIMARY KEY (id),
        CONSTRAINT ck_workspaces_id CHECK (id <> '')
    )
    """,
    f"""
    CREATE TABLE tether.issuers (
        workspace_id text NOT NULL,
        issuer_id text NOT NULL,
        iss text NOT NULL,
        audiences text[] NOT NULL,
        algorithm text NOT NULL,
        public_keys jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        CONSTRAINT pk_issuers PRIMARY KEY (workspace_id, issuer_id),
        CONSTRAINT uq_issuers_workspace_id_iss UNIQUE (workspace_id, iss),
        CONSTRAINT fk_issuers_workspace FOREIGN KEY (workspace_id)
            REFERENCES tether.workspaces (id),
        CONSTRAINT ck_issuers_algorithm CHECK (algorithm IN ('EdDSA', 'RS256')),
        CONSTRAINT ck_issuers_audiences CHECK (
            cardinality(audiences) >= 1 AND audiences <@ ARRAY[{_AUDIENCES}]::text[]
        )
    )
    """,
    """
    CREATE TABLE tether.api_keys (
        key_id text NOT NULL,
        workspace_id text NOT NULL,
        salt bytea NOT NULL,
        secret_hash bytea NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        CONSTRAINT pk_api_keys PRIMARY KEY (key_id),
        CONSTRAINT uq_api_keys_workspace_id_key_id UNIQUE (workspace_id, key_id),
        CONSTRAINT fk_api_keys_workspace FOREIGN KEY (workspace_id)
            REFERENCES tether.workspaces (id),
        CONSTRAINT ck_api_keys_key_id CHECK (key_id <> ''),
        CONSTRAINT ck_api_keys_salt CHECK (octet_length(salt) = 16),
        CONSTRAINT ck_api_keys_secret_hash CHECK (octet_length(secret_hash) = 32)
    )
    """,
    """
    CREATE TABLE tether.agents (
        workspace_id text NOT NULL,
        agent_id text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        CONSTRAINT pk_agents PRIMARY KEY (workspace_id, agent_id),
        CONSTRAINT fk_agents_workspace FOREIGN KEY (workspace_id)
            REFERENCES tether.workspaces (id),
        CONSTRAINT ck_agents_agent_id CHECK (agent_id <> '')
    )
    """,
    """
    CREATE TABLE tether.api_key_agents (
        workspace_id text NOT NULL,
        key_id text NOT NULL,
        agent_id text NOT NULL,
        CONSTRAINT pk_api_key_agents PRIMARY KEY (key_id, agent_id),
        CONSTRAINT fk_api_key_agents_api_key FOREIGN KEY (workspace_id, key_id)
            REFERENCES tether.api_keys (workspace_id, key_id),
        CONSTRAINT fk_api_key_agents_agent FOREIGN KEY (workspace_id, agent_id)
            REFERENCES tether.agents (workspace_id, agent_id)
    )
    """,
    """
    CREATE TABLE tether.policy_versions (
        workspace_id text NOT NULL,
        version text NOT NULL,
        active boolean NOT NULL DEFAULT false,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        CONSTRAINT pk_policy_versions PRIMARY KEY (workspace_id, version),
        CONSTRAINT fk_policy_versions_workspace FOREIGN KEY (workspace_id)
            REFERENCES tether.workspaces (id),
        CONSTRAINT ck_policy_versions_version CHECK (version <> '')
    )
    """,
    """
    CREATE UNIQUE INDEX uq_policy_versions_one_active
        ON tether.policy_versions (workspace_id) WHERE active
    """,
    # --- Runtime ---------------------------------------------------------------------------
    """
    CREATE TABLE tether.runs (
        id uuid NOT NULL,
        workspace_id text NOT NULL,
        agent_id text NOT NULL,
        requester_snapshot jsonb NOT NULL,
        task_input jsonb NOT NULL,
        status text NOT NULL DEFAULT 'queued',
        outcome_summary text,
        cancel_requested_at timestamptz,
        cancel_requested_by text,
        deadline_at timestamptz NOT NULL,
        deadline_exceeded_at timestamptz,
        driver_generation bigint NOT NULL DEFAULT 0,
        binding_key_id text,
        trace_parent text,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        CONSTRAINT pk_runs PRIMARY KEY (id),
        CONSTRAINT uq_runs_workspace_id_id UNIQUE (workspace_id, id),
        CONSTRAINT fk_runs_agent FOREIGN KEY (workspace_id, agent_id)
            REFERENCES tether.agents (workspace_id, agent_id),
        CONSTRAINT ck_runs_status CHECK (status IN (
            'queued', 'running', 'awaiting_approval', 'awaiting_tool',
            'awaiting_reconciliation', 'succeeded', 'failed', 'cancelled', 'timed_out',
            'abandoned'
        )),
        CONSTRAINT ck_runs_cancel_request CHECK (
            (cancel_requested_at IS NULL) = (cancel_requested_by IS NULL)
        ),
        CONSTRAINT ck_runs_driver_generation CHECK (driver_generation >= 0)
    )
    """,
    "CREATE INDEX ix_runs_workspace_id_created_at ON tether.runs (workspace_id, created_at)",
    f"""
    CREATE TABLE tether.approvals (
        id uuid NOT NULL,
        workspace_id text NOT NULL,
        run_id uuid NOT NULL,
        step_ordinal bigint NOT NULL,
        call_hash text NOT NULL,
        tool text NOT NULL,
        tool_version text NOT NULL,
        arguments_masked jsonb NOT NULL,
        preconditions_masked jsonb NOT NULL,
        rationale text,
        status text NOT NULL DEFAULT 'pending',
        expires_at timestamptz NOT NULL,
        decided_by text,
        decided_at timestamptz,
        decision_reason text,
        policy_version text,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        CONSTRAINT pk_approvals PRIMARY KEY (id),
        CONSTRAINT uq_approvals_run_id_step_ordinal UNIQUE (run_id, step_ordinal),
        CONSTRAINT fk_approvals_run FOREIGN KEY (workspace_id, run_id)
            REFERENCES tether.runs (workspace_id, id),
        CONSTRAINT ck_approvals_status CHECK (
            status IN ('pending', 'approved', 'rejected', 'expired', 'closed')
        ),
        CONSTRAINT ck_approvals_step_ordinal CHECK (step_ordinal >= 0),
        CONSTRAINT ck_approvals_call_hash CHECK (call_hash ~ {_HEX64}),
        CONSTRAINT ck_approvals_decision CHECK (
            (status IN ('approved', 'rejected'))
            = (decided_by IS NOT NULL AND decided_at IS NOT NULL)
        ),
        CONSTRAINT ck_approvals_pending_undecided CHECK (
            status <> 'pending' OR (decided_by IS NULL AND decided_at IS NULL)
        )
    )
    """,
    f"""
    CREATE TABLE tether.ledger_entries (
        id uuid NOT NULL,
        workspace_id text NOT NULL,
        run_id uuid NOT NULL,
        step_ordinal bigint NOT NULL,
        tool text NOT NULL,
        tool_version text NOT NULL,
        effect text NOT NULL,
        idempotency_support text NOT NULL,
        call_hash text NOT NULL,
        idempotency_key text NOT NULL,
        state text NOT NULL DEFAULT 'pending',
        attempts integer NOT NULL DEFAULT 0,
        outcome_payload_masked jsonb,
        outcome_reason text,
        resolution text,
        resolved_by text,
        justification text,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        CONSTRAINT pk_ledger_entries PRIMARY KEY (id),
        CONSTRAINT uq_ledger_entries_run_id_step_ordinal UNIQUE (run_id, step_ordinal),
        CONSTRAINT uq_ledger_entries_idempotency_key UNIQUE (idempotency_key),
        CONSTRAINT fk_ledger_entries_run FOREIGN KEY (workspace_id, run_id)
            REFERENCES tether.runs (workspace_id, id),
        CONSTRAINT ck_ledger_entries_state CHECK (state IN (
            'pending', 'dispatched', 'completed', 'failed', 'indeterminate', 'abandoned'
        )),
        CONSTRAINT ck_ledger_entries_resolution CHECK (resolution IS NULL OR resolution IN (
            'automatic', 'reconciled_applied', 'reconciled_not_applied', 'reconciled_abandon'
        )),
        CONSTRAINT ck_ledger_entries_effect CHECK (effect IN ('read', 'write')),
        CONSTRAINT ck_ledger_entries_idempotency_support CHECK (
            idempotency_support IN ('idempotent', 'non_idempotent')
        ),
        CONSTRAINT ck_ledger_entries_step_ordinal CHECK (step_ordinal >= 0),
        CONSTRAINT ck_ledger_entries_attempts CHECK (attempts >= 0),
        CONSTRAINT ck_ledger_entries_call_hash CHECK (call_hash ~ {_HEX64}),
        CONSTRAINT ck_ledger_entries_idempotency_key CHECK (
            idempotency_key ~ '^tth_[a-z2-7]{{52}}$'
        ),
        CONSTRAINT ck_ledger_entries_reconciliation CHECK (
            resolution IS NULL
            OR resolution NOT IN (
                'reconciled_applied', 'reconciled_not_applied', 'reconciled_abandon'
            )
            OR (resolved_by IS NOT NULL AND justification IS NOT NULL)
        )
    )
    """,
    f"""
    CREATE TABLE tether.audit_heads (
        run_id uuid NOT NULL,
        workspace_id text NOT NULL,
        seq bigint NOT NULL DEFAULT 0,
        last_mac text,
        key_id text NOT NULL,
        CONSTRAINT pk_audit_heads PRIMARY KEY (run_id),
        CONSTRAINT fk_audit_heads_run FOREIGN KEY (workspace_id, run_id)
            REFERENCES tether.runs (workspace_id, id),
        CONSTRAINT ck_audit_heads_seq CHECK (seq >= 0),
        CONSTRAINT ck_audit_heads_last_mac CHECK (last_mac IS NULL OR last_mac ~ {_HEX64})
    )
    """,
    f"""
    CREATE TABLE tether.audit_events (
        workspace_id text NOT NULL,
        run_id uuid NOT NULL,
        seq bigint NOT NULL,
        dedupe_key text NOT NULL,
        event_type text NOT NULL,
        actor text,
        tool text,
        payload_masked jsonb NOT NULL,
        policy_version text,
        trace_id text,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        prev_mac text,
        mac text NOT NULL,
        key_id text NOT NULL,
        CONSTRAINT pk_audit_events PRIMARY KEY (run_id, seq),
        CONSTRAINT uq_audit_events_run_id_dedupe_key UNIQUE (run_id, dedupe_key),
        CONSTRAINT fk_audit_events_run FOREIGN KEY (workspace_id, run_id)
            REFERENCES tether.runs (workspace_id, id),
        CONSTRAINT ck_audit_events_seq CHECK (seq >= 0),
        CONSTRAINT ck_audit_events_mac CHECK (mac ~ {_HEX64}),
        CONSTRAINT ck_audit_events_prev_mac CHECK (prev_mac IS NULL OR prev_mac ~ {_HEX64})
    )
    """,
    "CREATE INDEX ix_audit_events_workspace_id_actor ON tether.audit_events (workspace_id, actor)",
    "CREATE INDEX ix_audit_events_workspace_id_tool ON tether.audit_events (workspace_id, tool)",
    """
    CREATE INDEX ix_audit_events_workspace_id_created_at
        ON tether.audit_events (workspace_id, created_at)
    """,
    """
    CREATE TABLE tether.jobs (
        id uuid NOT NULL,
        kind text NOT NULL,
        workspace_id text NOT NULL,
        run_id uuid NOT NULL,
        ref_id uuid,
        payload jsonb NOT NULL DEFAULT '{}'::jsonb,
        run_after timestamptz NOT NULL DEFAULT clock_timestamp(),
        state text NOT NULL DEFAULT 'ready',
        lease_token uuid,
        lease_expires_at timestamptz,
        attempts integer NOT NULL DEFAULT 0,
        max_attempts integer NOT NULL,
        concurrency_key text,
        trace_parent text,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        CONSTRAINT pk_jobs PRIMARY KEY (id),
        CONSTRAINT fk_jobs_run FOREIGN KEY (workspace_id, run_id)
            REFERENCES tether.runs (workspace_id, id),
        CONSTRAINT ck_jobs_kind CHECK (
            kind IN ('advance_run', 'execute_tool', 'expire_approval', 'run_deadline')
        ),
        CONSTRAINT ck_jobs_state CHECK (state IN ('ready', 'leased', 'done', 'dead')),
        CONSTRAINT ck_jobs_attempts CHECK (attempts >= 0),
        CONSTRAINT ck_jobs_max_attempts CHECK (max_attempts >= 1),
        CONSTRAINT ck_jobs_lease CHECK (
            state <> 'leased' OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        )
    )
    """,
    "CREATE INDEX ix_jobs_state_run_after ON tether.jobs (state, run_after)",
    """
    CREATE TABLE tether.model_calls (
        id uuid NOT NULL,
        workspace_id text NOT NULL,
        run_id uuid NOT NULL,
        turn_ordinal integer NOT NULL,
        provider_role text NOT NULL,
        provider text NOT NULL,
        model text NOT NULL,
        attempt integer NOT NULL,
        occurrence_id uuid NOT NULL,
        status text NOT NULL,
        input_tokens integer,
        output_tokens integer,
        cost_usd numeric(12, 6),
        error_code text,
        started_at timestamptz NOT NULL,
        finished_at timestamptz,
        trace_id text,
        CONSTRAINT pk_model_calls PRIMARY KEY (id),
        CONSTRAINT uq_model_calls_occurrence_id UNIQUE (occurrence_id),
        CONSTRAINT fk_model_calls_run FOREIGN KEY (workspace_id, run_id)
            REFERENCES tether.runs (workspace_id, id),
        CONSTRAINT ck_model_calls_turn_ordinal CHECK (turn_ordinal >= 0),
        CONSTRAINT ck_model_calls_provider_role CHECK (
            provider_role IN ('primary', 'secondary')
        ),
        CONSTRAINT ck_model_calls_attempt CHECK (attempt >= 1),
        CONSTRAINT ck_model_calls_status CHECK (status IN ('succeeded', 'failed')),
        CONSTRAINT ck_model_calls_tokens CHECK (
            (input_tokens IS NULL OR input_tokens >= 0)
            AND (output_tokens IS NULL OR output_tokens >= 0)
        ),
        CONSTRAINT ck_model_calls_cost CHECK (cost_usd IS NULL OR cost_usd >= 0),
        CONSTRAINT ck_model_calls_finished_after_started CHECK (
            finished_at IS NULL OR finished_at >= started_at
        )
    )
    """,
    "CREATE INDEX ix_model_calls_run_id ON tether.model_calls (run_id)",
    f"""
    CREATE TABLE tether.access_events (
        id uuid NOT NULL,
        workspace_id text NOT NULL,
        actor text NOT NULL,
        audience text NOT NULL,
        action text NOT NULL,
        target_run_id uuid,
        details jsonb NOT NULL DEFAULT '{{}}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        CONSTRAINT pk_access_events PRIMARY KEY (id),
        CONSTRAINT fk_access_events_workspace FOREIGN KEY (workspace_id)
            REFERENCES tether.workspaces (id),
        CONSTRAINT ck_access_events_audience CHECK (audience IN ({_AUDIENCES}))
    )
    """,
    """
    CREATE INDEX ix_access_events_workspace_id_created_at
        ON tether.access_events (workspace_id, created_at)
    """,
    # --- Terminal-status guard (invariant 8, ADR-0006) ---------------------------------------
    f"""
    CREATE FUNCTION tether.runs_status_terminal_guard() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
        IF OLD.status IN ({_TERMINAL}) AND NEW.status IS DISTINCT FROM OLD.status THEN
            RAISE EXCEPTION 'run % has terminal status %; it cannot change', OLD.id, OLD.status
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END
    $$
    """,
    """
    CREATE TRIGGER runs_status_terminal_guard
        BEFORE UPDATE OF status ON tether.runs
        FOR EACH ROW EXECUTE FUNCTION tether.runs_status_terminal_guard()
    """,
    # --- Grants for the application role (explicit; independent of default privileges) ----
    *(f"REVOKE ALL ON tether.{table} FROM tether_app" for table in BOOTSTRAP_TABLES),
    *(f"REVOKE ALL ON tether.{table} FROM tether_app" for table in RUNTIME_TABLES),
    *(f"GRANT SELECT ON tether.{table} TO tether_app" for table in BOOTSTRAP_TABLES),
    # No DELETE anywhere for the app role (owner decisions): runtime code transitions rows and
    # never removes them. Any future retention/cleanup must use a separate, explicitly
    # privileged path. Audit and access events are append-only.
    "GRANT SELECT, INSERT, UPDATE ON tether.runs, tether.approvals, tether.ledger_entries,"
    " tether.model_calls, tether.audit_heads, tether.jobs TO tether_app",
    "GRANT SELECT, INSERT ON tether.audit_events, tether.access_events TO tether_app",
    # F2 / PD-31: row locks (SELECT ... FOR SHARE) need UPDATE privilege. A column grant on
    # created_at enables them without letting the app change any policy data.
    "GRANT UPDATE (created_at) ON tether.policy_versions TO tether_app",
    # F1: the migration bookkeeping table predates this revision and picked up init.sh's
    # default DML grants; it is read-only for the app role.
    "REVOKE ALL ON tether.alembic_version FROM tether_app",
    "GRANT SELECT ON tether.alembic_version TO tether_app",
]

DOWNGRADE = [
    "DROP TRIGGER runs_status_terminal_guard ON tether.runs",
    "DROP FUNCTION tether.runs_status_terminal_guard()",
    *(f"DROP TABLE tether.{table}" for table in reversed(RUNTIME_TABLES)),
    *(f"DROP TABLE tether.{table}" for table in reversed(BOOTSTRAP_TABLES)),
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)

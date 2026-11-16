"""P-9 and the G-13 reordering evidence: ``tether audit verify`` against a real Postgres chain.

Tampering is done the way someone with database access would do it (the superuser); the CLI runs
as the least-privilege application role, reading through the authoritative verifier. The tests
state the V1 limits too: ADR-0015 rule 8 (no detection of tail truncation or whole-run deletion
by someone who also rewrites or removes the head) is pinned here, not hidden.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import uuid
from collections.abc import Coroutine
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb
from typer.testing import CliRunner

from tether.cli.main import app
from tether.core.canonical import canonicalize
from tether.core.ids import WorkspaceId
from tether.secrets.env_provider import EnvSecretsProvider
from tether.secrets.keyring import KeyFamily, KeyRing
from tether.storage.engine import create_database_engine
from tether.storage.repos.audit import AuditRepository
from tether.storage.scope import WorkspaceScope
from tether.storage.uow import UnitOfWork

WS = "ws-a"
SCOPE = WorkspaceScope(WorkspaceId(WS))
KEYS = KeyRing(EnvSecretsProvider())
runner = CliRunner()
SENSITIVE = "SUPER-SECRET-PAYLOAD-VALUE"


def run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    # psycopg's async driver cannot use the Windows default (Proactor) loop.
    return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)


def append_events(
    db: Any, run_id: uuid.UUID, payloads: list[dict[str, Any]], start: int = 0
) -> None:
    async def go() -> None:
        engine = create_database_engine(db.app_dsn)
        try:
            for offset, payload in enumerate(payloads, start=start):
                async with UnitOfWork(engine) as uow:
                    await uow.repository(AuditRepository, SCOPE).append(
                        KEYS,
                        run_id=run_id,
                        dedupe_key=f"e{offset}",
                        event_type="PROBE",
                        payload=payload,
                    )
        finally:
            await engine.dispose()

    run_async(go())


def seed_v1(db: Any, run_id: uuid.UUID, count: int) -> None:
    """A valid version-1 chain written directly, as the code did before W2.4."""
    key_id = KEYS.active_key_id(KeyFamily.AUDIT)
    key = KEYS.key(KeyFamily.AUDIT, key_id).secret.reveal()
    prev_mac: str | None = None
    with psycopg.connect(db.migrator_dsn) as conn:
        for seq in range(1, count + 1):
            record = {
                "workspace_id": WS,
                "run_id": str(run_id),
                "seq": seq,
                "dedupe_key": f"legacy-{seq}",
                "event_type": "PROBE",
                "actor": None,
                "tool": None,
                "payload": {"n": seq},
                "policy_version": None,
                "prev_mac": prev_mac,
            }
            mac = hmac.new(key, canonicalize(record, allow_floats=True), hashlib.sha256).hexdigest()
            conn.execute(
                "INSERT INTO tether.audit_events (workspace_id, run_id, seq, dedupe_key,"
                " event_type, payload_masked, prev_mac, mac, key_id, mac_version)"
                " VALUES (%s, %s, %s, %s, 'PROBE', %s, %s, %s, %s, 1)",
                (WS, run_id, seq, f"legacy-{seq}", Jsonb({"n": seq}), prev_mac, mac, key_id),
            )
            prev_mac = mac
        conn.execute(
            "INSERT INTO tether.audit_heads (run_id, workspace_id, seq, last_mac, key_id)"
            " VALUES (%s, %s, %s, %s, %s)",
            (run_id, WS, count, prev_mac, key_id),
        )


def tamper(db: Any, *statements: tuple[str, tuple[Any, ...]]) -> None:
    """Privileged edits, as someone with database access would make them (one transaction)."""
    with psycopg.connect(db.superuser_dsn(db.dbname)) as admin:
        for statement, params in statements:
            admin.execute(statement, params)


@pytest.fixture
def cli(seeded: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("TETHER_DATABASE_URL", seeded.db.app_dsn)

    def verify(run_id: Any, workspace: str = WS) -> Any:
        return runner.invoke(
            app, ["audit", "verify", "--workspace", workspace, "--run", str(run_id)]
        )

    return verify


def lines(result: Any) -> list[str]:
    return result.output.splitlines()


# --- A and B. valid chains -------------------------------------------------------------------


def test_a_a_valid_v2_chain_verifies_and_reports_the_facts(seeded: Any, cli: Any) -> None:
    append_events(seeded.db, seeded.run_a, [{"n": 1}, {"n": 2}, {"n": 3}])
    result = cli(seeded.run_a)
    assert result.exit_code == 0, result.output
    out = lines(result)
    assert out[0] == "VALID"
    for expected in (
        f"workspace: {WS}",
        f"run: {seeded.run_a}",
        "events: 3",
        "last_seq: 3",
        "head: seq=3 consistent",
        "mac_versions: 2",
    ):
        assert expected in out


def test_b_a_mixed_v1_v2_chain_verifies_and_lists_both_versions(seeded: Any, cli: Any) -> None:
    seed_v1(seeded.db, seeded.run_a, 2)
    append_events(seeded.db, seeded.run_a, [{"n": 3}, {"n": 4}], start=3)
    result = cli(seeded.run_a)
    assert result.exit_code == 0, result.output
    assert "mac_versions: 1,2" in lines(result)
    assert "events: 4" in lines(result)


# --- C, D, F. tampering ----------------------------------------------------------------------


def test_c_a_changed_payload_is_invalid_at_that_event_and_leaks_nothing(
    seeded: Any, cli: Any
) -> None:
    append_events(seeded.db, seeded.run_a, [{"n": 1}, {"secret": SENSITIVE}, {"n": 3}])
    assert cli(seeded.run_a).exit_code == 0
    tamper(
        seeded.db,
        (
            "UPDATE tether.audit_events SET payload_masked = '{\"n\": 9}'"
            " WHERE run_id = %s AND seq = 2",
            (seeded.run_a,),
        ),
    )
    result = cli(seeded.run_a)
    assert result.exit_code == 1
    assert lines(result)[0] == "INVALID"
    assert "reason: mac_mismatch" in lines(result)
    assert "failed_seq: 2" in lines(result)
    assert SENSITIVE not in result.output


def test_d_a_changed_v2_timestamp_is_invalid(seeded: Any, cli: Any) -> None:
    append_events(seeded.db, seeded.run_a, [{"n": 1}, {"n": 2}, {"n": 3}])
    tamper(
        seeded.db,
        (
            "UPDATE tether.audit_events SET created_at = created_at + interval '1 second'"
            " WHERE run_id = %s AND seq = 3",
            (seeded.run_a,),
        ),
    )
    result = cli(seeded.run_a)
    assert result.exit_code == 1
    assert "reason: mac_mismatch" in lines(result)
    assert "failed_seq: 3" in lines(result)


@pytest.mark.parametrize("version", [3, 99])
def test_f_an_unsupported_mac_version_is_invalid_with_its_own_code(
    seeded: Any, cli: Any, version: int
) -> None:
    append_events(seeded.db, seeded.run_a, [{"n": 1}, {"n": 2}])
    tamper(
        seeded.db,
        (
            "UPDATE tether.audit_events SET mac_version = %s WHERE run_id = %s AND seq = 2",
            (version, seeded.run_a),
        ),
    )
    result = cli(seeded.run_a)
    assert result.exit_code == 1
    assert "reason: unsupported_mac_version" in lines(result)
    assert "failed_seq: 2" in lines(result)


def test_f_a_downgrade_from_v2_to_v1_is_invalid(seeded: Any, cli: Any) -> None:
    append_events(seeded.db, seeded.run_a, [{"n": 1}, {"n": 2}])
    tamper(
        seeded.db,
        (
            "UPDATE tether.audit_events SET mac_version = 1 WHERE run_id = %s AND seq = 2",
            (seeded.run_a,),
        ),
    )
    result = cli(seeded.run_a)
    assert result.exit_code == 1
    assert "reason: mac_mismatch" in lines(result)


# --- E. reordering (the G-13 evidence gap) ---------------------------------------------------


def swap_seq(run_id: uuid.UUID, first: int, second: int) -> list[tuple[str, tuple[Any, ...]]]:
    """Swap two events' sequence numbers. The primary key is (run_id, seq), so go through 1000."""
    sql = "UPDATE tether.audit_events SET seq = %s WHERE run_id = %s AND seq = %s"
    return [
        (sql, (1000, run_id, first)),
        (sql, (first, run_id, second)),
        (sql, (second, run_id, 1000)),
    ]


def test_e_swapping_two_events_sequence_numbers_is_invalid(seeded: Any, cli: Any) -> None:
    append_events(seeded.db, seeded.run_a, [{"n": 1}, {"n": 2}, {"n": 3}, {"n": 4}])
    assert cli(seeded.run_a).exit_code == 0
    tamper(seeded.db, *swap_seq(seeded.run_a, 2, 3))
    result = cli(seeded.run_a)
    assert result.exit_code == 1
    assert lines(result)[0] == "INVALID"
    # Sorted by seq, the old third event now sits second and links to the wrong predecessor.
    assert "reason: prev_mac_mismatch" in lines(result)
    assert "failed_seq: 2" in lines(result)


def test_e_a_swap_whose_links_are_repaired_still_fails_on_the_mac(seeded: Any, cli: Any) -> None:
    """Without the key the links can be made to look right, but the MAC covers seq and prev_mac."""
    append_events(seeded.db, seeded.run_a, [{"n": 1}, {"n": 2}, {"n": 3}])
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        macs = [
            row[0]
            for row in conn.execute(
                "SELECT mac FROM tether.audit_events WHERE run_id = %s ORDER BY seq",
                (seeded.run_a,),
            )
        ]
    relink = "UPDATE tether.audit_events SET prev_mac = %s WHERE run_id = %s AND seq = %s"
    tamper(
        seeded.db,
        *swap_seq(seeded.run_a, 2, 3),
        (relink, (macs[0], seeded.run_a, 2)),  # the old third event, now second, follows the first
        (relink, (macs[2], seeded.run_a, 3)),  # the old second event, now third, follows it
    )
    result = cli(seeded.run_a)
    assert result.exit_code == 1
    assert "reason: mac_mismatch" in lines(result)
    assert "failed_seq: 2" in lines(result)


def test_e_moving_an_event_to_a_later_sequence_number_leaves_a_gap_and_is_invalid(
    seeded: Any, cli: Any
) -> None:
    append_events(seeded.db, seeded.run_a, [{"n": 1}, {"n": 2}, {"n": 3}])
    tamper(
        seeded.db,
        ("UPDATE tether.audit_events SET seq = 5 WHERE run_id = %s AND seq = 3", (seeded.run_a,)),
    )
    result = cli(seeded.run_a)
    assert result.exit_code == 1
    assert "reason: sequence_mismatch" in lines(result)
    assert "failed_seq: 5" in lines(result)


def test_e_moving_the_first_event_to_the_end_is_invalid(seeded: Any, cli: Any) -> None:
    append_events(seeded.db, seeded.run_a, [{"n": 1}, {"n": 2}, {"n": 3}])
    tamper(
        seeded.db,
        ("UPDATE tether.audit_events SET seq = 4 WHERE run_id = %s AND seq = 1", (seeded.run_a,)),
    )
    result = cli(seeded.run_a)
    assert result.exit_code == 1
    assert "reason: sequence_mismatch" in lines(result)
    assert "failed_seq: 2" in lines(result)


# --- G and H. missing runs and workspace isolation -------------------------------------------


def test_g_an_unknown_run_is_a_safe_not_found(seeded: Any, cli: Any) -> None:
    result = cli(uuid.uuid4())
    assert result.exit_code == 4
    assert lines(result)[0] == "NOT_FOUND"
    assert "events:" not in result.output


def test_h_a_run_in_another_workspace_looks_exactly_like_an_unknown_run(
    seeded: Any, cli: Any
) -> None:
    append_events(seeded.db, seeded.run_a, [{"secret": SENSITIVE}, {"n": 2}])
    assert cli(seeded.run_a).exit_code == 0
    crossed = cli(seeded.run_a, workspace="ws-b")
    unknown = cli(uuid.uuid4(), workspace="ws-b")
    assert crossed.exit_code == unknown.exit_code == 4
    assert lines(crossed)[0] == lines(unknown)[0] == "NOT_FOUND"
    for leaked in ("events:", "last_seq:", "mac_versions:", "head:", SENSITIVE):
        assert leaked not in crossed.output


# --- I. nothing sensitive in output ----------------------------------------------------------


def test_i_no_key_material_or_connection_secret_is_ever_printed(
    seeded: Any, cli: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    append_events(seeded.db, seeded.run_a, [{"secret": SENSITIVE}, {"n": 2}])
    key_id = KEYS.active_key_id(KeyFamily.AUDIT)
    key_text = os.environ[f"TETHER_AUDIT_KEY_{key_id}"]
    app_password = seeded.db.app_password
    tamper(
        seeded.db,
        (
            "UPDATE tether.audit_events SET payload_masked = '{\"x\": 1}'"
            " WHERE run_id = %s AND seq = 1",
            (seeded.run_a,),
        ),
    )
    outputs = [cli(seeded.run_a).output]
    monkeypatch.delenv(f"TETHER_AUDIT_KEY_{key_id}")
    outputs.append(cli(seeded.run_a).output)  # key unavailable: operational failure
    monkeypatch.setenv("TETHER_DATABASE_URL", f"postgresql://x:{app_password}@127.0.0.1:1/none")
    outputs.append(cli(seeded.run_a).output)  # database unreachable
    for output in outputs:
        for secret in (SENSITIVE, key_text, app_password):
            assert secret not in output


def test_a_missing_audit_key_is_exit_three_not_an_invalid_chain(
    seeded: Any, cli: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    append_events(seeded.db, seeded.run_a, [{"n": 1}])
    monkeypatch.delenv(f"TETHER_AUDIT_KEY_{KEYS.active_key_id(KeyFamily.AUDIT)}")
    result = cli(seeded.run_a)
    assert result.exit_code == 3
    assert "INVALID" not in result.output


def test_an_unreachable_database_is_exit_three(
    seeded: Any, cli: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TETHER_DATABASE_URL", "postgresql://x:y@127.0.0.1:1/none")
    result = cli(seeded.run_a)
    assert result.exit_code == 3
    assert "INVALID" not in result.output


# --- the persisted head: what it adds and what it does not -----------------------------------


def test_deleting_the_last_event_while_the_head_still_names_it_is_invalid(
    seeded: Any, cli: Any
) -> None:
    """The one truncation case the stored head exposes: events removed, head left as it was."""
    append_events(seeded.db, seeded.run_a, [{"n": 1}, {"n": 2}, {"n": 3}])
    tamper(
        seeded.db,
        ("DELETE FROM tether.audit_events WHERE run_id = %s AND seq = 3", (seeded.run_a,)),
    )
    result = cli(seeded.run_a)
    assert result.exit_code == 1
    assert "reason: head_mismatch" in lines(result)


def test_a_head_whose_mac_does_not_match_its_event_is_invalid(seeded: Any, cli: Any) -> None:
    append_events(seeded.db, seeded.run_a, [{"n": 1}, {"n": 2}])
    tamper(
        seeded.db,
        (
            "UPDATE tether.audit_heads SET last_mac = %s WHERE run_id = %s",
            ("c" * 64, seeded.run_a),
        ),
    )
    result = cli(seeded.run_a)
    assert result.exit_code == 1
    assert "reason: head_mismatch" in lines(result)


def test_v1_limit_truncation_with_a_rewritten_head_is_not_detected(seeded: Any, cli: Any) -> None:
    """ADR-0015 rule 8, pinned: whoever can delete events can also rewrite the head to match."""
    append_events(seeded.db, seeded.run_a, [{"n": 1}, {"n": 2}, {"n": 3}])
    with psycopg.connect(seeded.db.migrator_dsn) as conn:
        second_mac = conn.execute(
            "SELECT mac FROM tether.audit_events WHERE run_id = %s AND seq = 2", (seeded.run_a,)
        ).fetchone()
    assert second_mac is not None
    tamper(
        seeded.db,
        ("DELETE FROM tether.audit_events WHERE run_id = %s AND seq = 3", (seeded.run_a,)),
        (
            "UPDATE tether.audit_heads SET seq = 2, last_mac = %s WHERE run_id = %s",
            (second_mac[0], seeded.run_a),
        ),
    )
    result = cli(seeded.run_a)
    assert result.exit_code == 0  # a shorter, internally consistent chain verifies


def test_v1_limit_whole_run_deletion_is_indistinguishable_from_an_unknown_run(
    seeded: Any, cli: Any
) -> None:
    append_events(seeded.db, seeded.run_a, [{"n": 1}, {"n": 2}])
    tamper(
        seeded.db,
        ("DELETE FROM tether.audit_events WHERE run_id = %s", (seeded.run_a,)),
        ("DELETE FROM tether.audit_heads WHERE run_id = %s", (seeded.run_a,)),
    )
    assert cli(seeded.run_a).exit_code == 4


def test_the_verifier_reads_only_and_changes_nothing(seeded: Any, cli: Any) -> None:
    append_events(seeded.db, seeded.run_a, [{"n": 1}, {"n": 2}])

    def snapshot() -> list[Any]:
        with psycopg.connect(seeded.db.migrator_dsn) as conn:
            events = conn.execute(
                "SELECT * FROM tether.audit_events WHERE run_id = %s ORDER BY seq", (seeded.run_a,)
            ).fetchall()
            heads = conn.execute(
                "SELECT * FROM tether.audit_heads WHERE run_id = %s", (seeded.run_a,)
            ).fetchall()
        return [events, heads]

    before = snapshot()
    assert cli(seeded.run_a).exit_code == 0
    assert snapshot() == before


def test_verify_run_stays_chain_only_while_the_cli_report_adds_the_head_check(
    seeded: Any, cli: Any
) -> None:
    """The API's ``verify_run`` must not become stricter: only the report path reads the head."""
    from tether.audit.chain import verify_run, verify_run_report

    append_events(seeded.db, seeded.run_a, [{"n": 1}, {"n": 2}, {"n": 3}])
    tamper(
        seeded.db,
        ("DELETE FROM tether.audit_events WHERE run_id = %s AND seq = 3", (seeded.run_a,)),
    )

    async def both() -> tuple[bool, Any]:
        engine = create_database_engine(seeded.db.app_dsn)
        try:
            async with UnitOfWork(engine) as uow:
                repo = uow.repository(AuditRepository, SCOPE)
                return (
                    await verify_run(repo, KEYS, seeded.run_a),
                    await verify_run_report(repo, KEYS, seeded.run_a),
                )
        finally:
            await engine.dispose()

    boolean, report = run_async(both())
    assert boolean is True  # unchanged behavior: the remaining chain is internally valid
    assert report.valid is False
    assert report.reason == "head_mismatch"
    assert cli(seeded.run_a).exit_code == 1

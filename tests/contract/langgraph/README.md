# LangGraph semantics contract suite (S1.3)

These tests pin the LangGraph behaviour that ADR-0003 and ADR-0005 rely on, against the exact
versions in `pyproject.toml` (PD-22):

- `langgraph==1.2.14`
- `langgraph-checkpoint-postgres==3.1.2`

Run with `uv run poe test-contract`. It starts `deploy/compose/docker-compose.test.yml` and loads
`deploy/compose/.env`. **Run it on every LangGraph or saver version bump.** If any of (a)–(e) fails,
ADR-0003's premises no longer hold: stop, and raise a superseding ADR before the run driver (S2.8)
depends on the new behaviour.

All tests use the production saver factory (`tether.runtime.checkpointer`) connected as the
`tether_app` role, as the worker will be.

| Contract | Test | What it pins |
|---|---|---|
| (a) | `test_durability_sync.py` | With `durability="sync"`, a node reads the previous step's checkpoint as committed, through an independent connection. A control shows `durability="exit"` does not, so the probe can fail. |
| (b) | `test_interrupt_resume.py` | Resuming re-executes the interrupted node from its first line. A spurious wake (condition still unsatisfied) loops and pauses again. |
| (c) | `test_interrupt_resume.py` | A constant resume value is accepted and carries no meaning. `Command(resume=None)` is **not** a valid resume on the pinned version (see below). |
| (d) | `test_crash_resume.py` | After `os._exit` inside a node (in a subprocess), `invoke(None)` on the same thread re-runs that node only; completed nodes do not run again. |
| (e) | `test_state_inspection.py` | State inspection distinguishes the four §9.3 step-3 branches (mapping below). |
| (f), schema half | `tests/integration/test_compose_smoke.py` | The saver's tables exist only in the `langgraph` schema after `tether migrate`. |
| (f), autogenerate half | moved to S1.5 | "Alembic autogenerate ignores the saver tables" needs `target_metadata`, which first exists in S1.5 (plan S1.3/S1.5). |

## Invocation choice (architecture §9.3 step 3)

From `graph.aget_state(config)` (a `StateSnapshot`), checked in this order:

| Condition | Branch | Driver action |
|---|---|---|
| `snapshot.created_at is None` | no checkpoint | invoke with the initial input built from `runs` |
| `snapshot.interrupts` non-empty | pending interrupt | `invoke(Command(resume=WAKE))` |
| `snapshot.next` non-empty | crash mid-step | `invoke(None)` |
| otherwise | finished | reconcile status via TX11 finalize |

Interrupts are checked before `next`: a paused thread also has a non-empty `next`.

## Resume value

The driver resumes pending interrupts with an opaque, non-semantic **sentinel**, currently
`"tether:wake"` (`WAKE` in `test_interrupt_resume.py`; architecture §9.3, amended 2026-10-08).
It carries no business or domain meaning: wait nodes ignore it and always re-read the
authoritative row (ADR-0003).

§9.3 originally prescribed `Command(resume=None)`. On `langgraph==1.2.14`, `None` is the
constructor default for `resume`, and resuming with it raises `UnboundLocalError` inside
LangGraph instead of resuming. `test_resume_with_none_is_not_a_valid_resume_on_pinned_version`
pins that behaviour; if a future version accepts `None`, it fails, which is the cue to revisit.

## Saver connection requirements

Taken from `PostgresSaver.from_conn_string` on the pinned version and applied by the factory:
`autocommit=True` (its setup uses `CREATE INDEX CONCURRENTLY`), `prepare_threshold=0`,
`row_factory=dict_row`. The saver's SQL is not schema-qualified, so every saver connection also
sets `search_path=langgraph`; the database roles default to `tether`.

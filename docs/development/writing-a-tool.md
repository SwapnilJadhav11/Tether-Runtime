# Writing a tool

This guide is for engineers who add tools to Tether: what a workload is, how to declare a tool, how its handler reports outcomes, and how to test it. A tool is the only way an agent changes the outside world, so most of the contract is about saying honestly what happened.

## Workloads and the import rule

A **workload** is a package that supplies a set of tools and one agent definition. Workloads are clients of Tether, never part of it. Two ship in the repository: `workloads/incident_remediation/` and `workloads/change_control/`.

A workload exposes one `tether.sdk.Workload` object:

```python
WORKLOAD = Workload(
    name="flag_admin",
    tools=(set_flag, get_flag),
    agent=AgentDefinition(system_prompt="...", tools=("get_flag", "set_flag")),
    retained_tools=(),   # earlier versions that must stay resolvable
)
```

It registers through the `tether.workloads` entry-point group, in the package's `pyproject.toml`:

```toml
[project.entry-points."tether.workloads"]
flag_admin = "workloads.flag_admin:WORKLOAD"
```

The entry-point name must equal `Workload.name`, and a bootstrap agent names the workload by that name (`agents: [{id: ..., workload: flag_admin}]`). The worker loads each workload an agent names at startup, and refuses to start if one is missing or does not provide a matching `Workload`. The API process does not load workloads. The workload must therefore be installed in the image that runs the worker.

`Workload` rejects duplicate tool names, an agent that offers an unknown tool, and a `(name, version)` pair declared twice.

**Import only `tether.sdk`.** A workload may not import any other module from Tether, and the core never imports a workload ([ADR-0002](../adr/0002-core-sdk-workload-layering.md)). Import-linter contracts and the tests in `tests/architecture/` enforce both directions, and a scanner keeps workload-specific names out of `src/tether/`. Third-party libraries (such as `httpx` and `pydantic`) are fine. Adding a top-level `tether.<module>` means listing it in the workload `forbidden_modules` contract in `pyproject.toml`.

## The tool descriptor

A `ToolDescriptor` is immutable and validated when it is constructed: a bad combination raises `ValueError` at import time, not on a live run.

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | `[a-z][a-z0-9_]{0,63}`. The model addresses the tool by this name. |
| `version` | yes | A non-empty string. Bound into every call hash and approval; see [versions](#versions). |
| `description` | yes | Shown to the model. Say what the tool does and whether it needs approval. |
| `input_model` | yes | A Pydantic model. Every proposal is validated against it before anything else happens. Prefer `ConfigDict(extra="forbid")`. |
| `output_model` | yes | A Pydantic model. The handler's return value is validated against it. |
| `effect` | yes | `read` or `write`. |
| `handler` | yes | `async def handler(args, ctx) -> BaseModel`. |
| `risk` | yes | `low` or `high`. A `high` tool can never have an `allow` rule: worker startup refuses that policy. |
| `required_permissions` | yes | At least one `resource:action` string (lowercase, matched exactly, no wildcards). |
| `idempotency` | writes | `supported` or `unsupported`. Leave unset for reads. |
| `max_attempts` | no, default `3` | Send budget for one call, shared by retries and redeliveries. |
| `timeout_seconds` | no, default `10.0` | One send. Greater than 0 and at most 300. |
| `on_exhausted` | no, default `typed_failure` | Reads only: `typed_failure` or `fail_run`. |
| `preconditions` | no | Writes only: top-level input fields the target checks atomically. |
| `verify` | no | Writes only: a read-back that can resolve an unknown outcome. |

### What the choices make Tether do

**`effect`.** A read has no side effect, so Tether may repeat it. A write changes the world, so Tether is careful with it.

**`risk` and `required_permissions`.** These are metadata for [policy](../governance/policy.md), fixed by the tool and never taken from the model. A call is permitted only if the requester's permissions (from their roles) intersected with the agent's permissions cover `required_permissions`.

**`idempotency`.** This declares whether the target honours the idempotency key that Tether forwards.

| Value | Meaning | After an unknown outcome |
|---|---|---|
| `supported` | The target applies a request once per key and answers a repeat with the original result. | Tether re-sends the same call with the same key, up to `max_attempts`, each time after recording the re-send. When the budget is spent the entry is `indeterminate`. |
| `unsupported` | The target has no such mechanism. | Tether never re-sends. The entry is `indeterminate` at once. |

Declare `supported` only when the target really deduplicates by key. The guarantee is only as strong as that claim: [side effects and the ledger](../governance/side-effects-and-ledger.md) explains the reasoning, and [ADR-0008](../adr/0008-idempotency-keys-and-stop-gated-resends.md) the rules.

**`max_attempts`.** Counts every send of one governed call, whether a retry within a delivery or a redelivery after a worker died. A non-idempotent write is sent at most once whatever this is set to.

**`timeout_seconds`.** Bounds one send. A timeout is always an unknown outcome, never `not_applied`. The timeout is not clipped to the run deadline. The worker extends its lease on the job while the handler runs, so a long timeout is safe, but the handler must be `async` and must not block the event loop: a blocking call would starve that heartbeat.

**`on_exhausted`.** For a read whose outcome stays unknown after its budget. `typed_failure` hands the model a `failed` result and the run continues. `fail_run` ends the run `failed` with the summary `tool_exhausted:<tool>`. If an unresolved write exists, the run is held first. Use `fail_run` for a read the run cannot sensibly continue without.

**`Sensitive`.** Mark secret fields on the input or output model:

```python
from typing import Annotated
from tether.sdk import Sensitive

class RotateIn(BaseModel):
    service: str
    new_token: Annotated[str, Sensitive]
```

The handler receives the real value, and the call hash covers it. Everywhere else it is replaced with a mask before it is stored, audited, logged, or shown to an approver or the model: approvals, ledger outputs, audit payloads and inspection views. Masking happens at one choke point ([ADR-0017](../adr/0017-sensitive-data-masking-choke-point.md)). Tool authors mark fields; they do not mask by hand.

**`preconditions`.** For a write whose correctness depends on the state it was decided against. Name the input fields that carry that expectation (they must be required and have no default). Their names and values are bound into the approval, so the approver sees what the write assumes. **The target must enforce them atomically with the write**, and apply nothing on a mismatch. Tether never reads the target's current state. A read-then-write inside the handler is not a precondition. See `update_service_config` in `workloads/incident_remediation/tools.py`, which sends `expected_config_version` and treats the target's `409` as not applied.

## Reporting outcomes

A handler reports what happened by how it ends.

| The handler | Tether records | Notes |
|---|---|---|
| returns an `output_model` instance | `applied` | The output is validated, masked and stored. A write whose output violates the schema is still `applied`, its output is withheld, and it is never re-sent. A read with invalid output is a typed `failed`. |
| raises `ToolNotApplied(reason)` | `not_applied` | Only for positive evidence that nothing changed. |
| raises anything else, or times out | `unknown` | A read is retried up to its budget, then fails. An idempotent write is re-sent with the same key up to its budget, then becomes `indeterminate`. A non-idempotent write becomes `indeterminate` immediately. |

An `indeterminate` entry holds the run in `awaiting_reconciliation`. If the tool declares `verify`, a bounded read-back runs first. Otherwise, or if the read-back is inconclusive, a person resolves it. See [unknown outcomes](../governance/unknown-outcomes.md).

**Raise `ToolNotApplied` only when you are certain.** It is the one signal that lets Tether say a write did not happen, and the model is told so. A `404` or `422` from the target that rejected the request before acting is evidence. A `5xx`, a rate limit (`403` or `429`), a timeout, a dropped connection or an unexpected status is not: the target may have applied the change before failing. Let those exceptions propagate. The change-control tools show the line precisely: `404`, `410` and `422` raise `ToolNotApplied`; `raise_for_status()` handles the rest; and a success status other than the expected one is also raised as unknown.

Do not catch broad exceptions and return a default, and do not translate an error into a successful-looking output. Either one converts an unknown outcome into a false `applied`.

## Using the idempotency key

`ToolContext.idempotency_key` is deterministic: it derives from the run, the step and the call hash, so every send of the same governed call carries the same key, and a different call never does. Forward it on every write, in whatever mechanism the target offers. The incident workload sends it as an `Idempotency-Key` header. For a `supported` tool, the target must:

- apply the first request with a key and record the result under it;
- answer a repeat of the same request with the original result;
- reject the same key with a different request, rather than apply it.

If the target cannot do this, declare `unsupported`. If the target has a different natural idempotence (the change-control `apply_label` is naturally idempotent on GitHub), `supported` is still correct.

Never put the key, or any model-supplied text, into a URL path or header without constraining it. Constrain input fields with patterns or `Literal` types, and send model-supplied text only as JSON.

## A safe read-back

A write may declare how to find out what happened after an unknown outcome:

```python
VerifyDeclaration(
    probe=_verify,                      # async (args, VerifyContext) -> VerificationResult
    evidence_model=FlagCheck,           # Pydantic model; mark sensitive fields
    required_permissions=("flag:read",),
    max_attempts=3,                     # default
    backoff=VerifyBackoff(),            # base 1 s, x2, cap 30 s; the default
)
```

The probe receives the original arguments and a `VerifyContext` that holds the same idempotency key. It answers with a `VerificationResult(kind, evidence)`:

| `kind` | Say it only when | Effect |
|---|---|---|
| `confirmed_applied` | The target shows the change took effect. | The entry resolves `verified_applied`; the run continues. |
| `confirmed_not_applied` | The target gives definitive evidence that the change did not happen. | The entry resolves `verified_not_applied`. |
| `inconclusive` | Anything else. | Another probe after the backoff, up to `max_attempts`; then a person reconciles. |

Rules for a probe:

- **Reads only.** It must never re-issue the write or change the target. It may only look.
- **Ask about this call.** Find the effect by the idempotency key or an equivalent marker, not by "something like it exists".
- **Absence is weak evidence.** A target that is eventually consistent can fail to show a change that has applied. The change-control probe finds its marker in an issue listing as proof of `applied`, but concludes `not_applied` only after two complete listings 15 seconds apart both lack it. An incomplete listing, a non-`200` status, a configuration problem or any exception is `inconclusive`. When in doubt, answer `inconclusive`: a person resolves it, and a wrong `confirmed_not_applied` would let the run act on a write that did happen.
- **It runs under policy.** The probe runs only if current policy allows the verify operation: the tool must not be `deny`, and the requester's and agent's permissions must cover the declaration's `required_permissions`. A probe needs read permissions only. The approval that authorized the write is not authority for it ([ADR-0022](../adr/0022-verify-operation-policy-mapping.md)).
- **A person overrides.** If a person reconciles an entry while a probe is running, the person's decision wins.

A probe is bound by the tool's `timeout_seconds`; one that outlives it counts as inconclusive. The design is in [ADR-0019](../adr/0019-automatic-verification-of-unknown-outcomes.md).

## Versions

A tool's `(name, version)` is bound into the call hash and the approval ([ADR-0011](../adr/0011-tool-version-pinning-approval-validity.md)). Once the model proposes a call, Tether pins the offered version, and every later step for that call resolves exactly that version: never a newer one.

- **Bump the version whenever behaviour, schema, risk or permissions change.** Tether does not hash the handler's code, so a changed descriptor with the same version would run under an approval given for the old behaviour.
- **Keep the old version in `Workload.retained_tools`** for as long as calls pinned to it may still be in flight. Retained versions resolve but are never offered to a new model turn. `incident_remediation` keeps `restart_service` `1.0.0` beside the current `1.1.0` (which added a read-back).
- If a pinned version is gone before anything was recorded, the call gets `not_applied` and nothing is sent. If it is gone after dispatch, the outcome is unknown and the run is held for a person.

## Policy for the new tool

A tool with no rule is denied. Add a rule to the workspace's `policy` in the bootstrap file, and grant the permissions the tool requires:

```yaml
policy:
  rules:
    - tool: get_flag
      decision: allow
    - tool: set_flag
      decision: require_approval
      approver_roles: [approver]
  role_permissions:
    operator: [flag:read, flag:write]
  agent_permissions:
    flag-admin: [flag:read, flag:write]
```

`set_flag` is `risk="high"`, so `allow` would be refused at startup. Re-run `tether bootstrap apply` after any policy change: the policy version is a hash of the policy and the evaluator version, and a decision is made only under the active version. See [policy](../governance/policy.md) and [approvals](../governance/approvals.md). `config/bootstrap/dev.yaml` and `config/bootstrap/change-control.yaml` are complete working examples.

## A complete example

A small workload with one read and one write against a hypothetical flag service. The write is idempotent, has a precondition, and has a read-back.

```python
"""workloads/flag_admin/tools.py"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, ConfigDict, Field

from tether.sdk import (
    ToolContext,
    ToolDescriptor,
    ToolNotApplied,
    VerificationResult,
    VerifyContext,
    VerifyDeclaration,
)

BASE_URL = "https://flags.example.internal"
_transport: httpx.AsyncBaseTransport | None = None


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Tests route requests to a fake instead of the network."""
    global _transport
    _transport = transport


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE_URL, transport=_transport, timeout=10.0)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FlagRef(_Strict):
    flag: str = Field(pattern=r"^[a-z0-9-]{1,64}$")


class SetFlagIn(_Strict):
    flag: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    enabled: bool
    expected_revision: int = Field(description="The revision this change was decided against")


class FlagState(BaseModel):
    flag: str
    enabled: bool
    revision: int


class FlagCheck(BaseModel):
    flag: str
    applied: bool | None


async def _get_flag(args: FlagRef, ctx: ToolContext) -> FlagState:
    async with _client() as http:
        response = await http.get(f"/flags/{args.flag}")
    if response.status_code == 404:
        raise ToolNotApplied("unknown flag")  # the target looked and found nothing
    response.raise_for_status()  # anything else is an unknown outcome
    return FlagState.model_validate(response.json())


async def _set_flag(args: SetFlagIn, ctx: ToolContext) -> FlagState:
    async with _client() as http:
        response = await http.put(
            f"/flags/{args.flag}",
            json={"enabled": args.enabled, "expected_revision": args.expected_revision},
            headers={"Idempotency-Key": ctx.idempotency_key},
        )
    # The target checks the revision atomically and applies nothing on a mismatch (409).
    if response.status_code in (404, 409, 422):
        raise ToolNotApplied(f"rejected ({response.status_code})")
    response.raise_for_status()
    return FlagState.model_validate(response.json())


async def _verify_set_flag(args: SetFlagIn, ctx: VerifyContext) -> VerificationResult:
    """Reads only: did a change with this key apply?"""
    async with _client() as http:
        response = await http.get(f"/flags/{args.flag}/changes/{ctx.idempotency_key}")
    applied = response.json().get("applied") if response.status_code == 200 else None
    if not isinstance(applied, bool):
        return VerificationResult("inconclusive", FlagCheck(flag=args.flag, applied=None))
    evidence = FlagCheck(flag=args.flag, applied=applied)
    return VerificationResult("confirmed_applied" if applied else "confirmed_not_applied", evidence)


get_flag = ToolDescriptor(
    name="get_flag",
    version="1.0.0",
    description="Read a feature flag's state and revision.",
    input_model=FlagRef,
    output_model=FlagState,
    effect="read",
    handler=_get_flag,
    risk="low",
    required_permissions=("flag:read",),
)

set_flag = ToolDescriptor(
    name="set_flag",
    version="1.0.0",
    description="Turn a feature flag on or off if it is still at the expected revision. "
    "Requires approval.",
    input_model=SetFlagIn,
    output_model=FlagState,
    effect="write",
    idempotency="supported",
    handler=_set_flag,
    risk="high",
    required_permissions=("flag:write",),
    preconditions=("expected_revision",),
    verify=VerifyDeclaration(
        probe=_verify_set_flag,
        evidence_model=FlagCheck,
        required_permissions=("flag:read",),
    ),
)
```

Two details matter. `_verify_set_flag` answers `confirmed_not_applied` only because this target records every applied change by key and answers definitively either way; a target without that would return `inconclusive` for a missing record. And `_get_flag` raises `ToolNotApplied` for `404` because a read that found nothing changed nothing.

## Testing a tool

Test at three levels. All of the following are valid with the repository's pytest settings (`asyncio_mode = "strict"`, so async tests need `@pytest.mark.asyncio`).

**1. The handler, against a fake transport.** Call `descriptor.handler` directly and put a fake behind the client, as `tests/unit/test_change_control_workload.py` does with `tests/support/fake_github.py`:

```python
import httpx
import pytest

from tether.sdk import ToolContext, ToolNotApplied
from workloads.flag_admin import tools

CTX = ToolContext(idempotency_key="tth_test_key")
ARGS = tools.SetFlagIn(flag="checkout-v2", enabled=True, expected_revision=4)


@pytest.mark.asyncio
async def test_a_stale_revision_is_not_applied() -> None:
    tools.set_transport(httpx.MockTransport(lambda request: httpx.Response(409)))
    with pytest.raises(ToolNotApplied):
        await tools.set_flag.handler(ARGS, CTX)


@pytest.mark.asyncio
async def test_a_server_error_is_unknown_not_not_applied() -> None:
    tools.set_transport(httpx.MockTransport(lambda request: httpx.Response(503)))
    with pytest.raises(httpx.HTTPStatusError):  # propagates: Tether records `unknown`
        await tools.set_flag.handler(ARGS, CTX)


@pytest.mark.asyncio
async def test_the_idempotency_key_is_forwarded() -> None:
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Idempotency-Key"])
        return httpx.Response(200, json={"flag": "checkout-v2", "enabled": True, "revision": 5})

    tools.set_transport(httpx.MockTransport(respond))
    await tools.set_flag.handler(ARGS, CTX)
    assert seen == ["tth_test_key"]
```

Cover each row of the outcome table, the forwarded key, input rejection (unknown fields, bad patterns), and every answer the probe can give, including a non-`200` and a malformed body, both of which must be `inconclusive`. Reset the transport after each test (`set_transport(None)`) in a fixture.

**2. Descriptor validation.** Assert that an invalid combination fails at construction (a write without `idempotency`, a precondition on a read, a `high`-risk tool offered under an `allow` rule), and that the workload loads by entry-point name.

**3. A governed run.** `tests/integration/test_change_control_runs.py` shows the pattern: load the real bootstrap file, run a scripted scenario through a real `WorkerRuntime` on a test database, approve through the API, and assert on the ledger, the audit events and the number of requests the fake saw ("exactly one send"). It also shows the failure cases that matter for a write tool: a timeout leaves an `indeterminate` entry after exactly one request; a crash after dispatch sends no second request; a target rejection is a definite `not_applied`; a read-back resolves an unknown outcome without a person. [Testing](testing.md) covers how these suites run.

Tests are written first and never weakened to pass.

## Checklist

- [ ] Imports only `tether.sdk` from Tether; the entry point name equals `Workload.name`.
- [ ] `effect`, `risk` and `required_permissions` are right; a write that can do harm is `high`.
- [ ] `idempotency` is `supported` only if the target deduplicates by the key, and the handler forwards the key.
- [ ] Input models reject unknown fields and constrain anything used in a URL.
- [ ] `ToolNotApplied` is raised only on positive evidence; every other failure propagates.
- [ ] Nothing catches broad exceptions and returns a default.
- [ ] Preconditions, if any, are enforced atomically by the target.
- [ ] `Sensitive` marks every secret field in input and output models.
- [ ] `timeout_seconds` fits the target; the handler is `async` and never blocks.
- [ ] A read-back, if declared, only reads, and answers `inconclusive` when unsure.
- [ ] Behaviour or schema changed: version bumped, old version in `retained_tools`.
- [ ] A policy rule, role permissions and agent permissions exist, and `tether bootstrap apply` was re-run.
- [ ] Tests cover every outcome and the probe's answers.

## Related

- [Policy](../governance/policy.md)
- [Side effects and the ledger](../governance/side-effects-and-ledger.md)
- [Unknown outcomes](../governance/unknown-outcomes.md)
- [Testing](testing.md)

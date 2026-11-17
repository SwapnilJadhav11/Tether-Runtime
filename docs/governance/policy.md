# Policy

How Tether decides whether a tool call may run, who may approve it, who may reconcile its outcome,
and whether its outcome may be verified automatically. This page is for engineers writing a
bootstrap file and for reviewers checking that a deployment fails closed. The policy lives in the
[bootstrap file](../operations/configuration.md); every other setting is documented there.

## The model in one paragraph

Policy is **deny by default**. Each workspace has a declarative policy section in its bootstrap
file: one rule per tool, the permissions each requester role grants, and the permissions each agent
holds. When the model proposes a tool call, Tether computes one of three decisions: `allow`, `deny`
or `require_approval`. The decision uses only trusted inputs, never anything the model wrote. It is
made only under the workspace's active policy version, and it is recorded, with its reasons, in the
run's audit chain before anything else happens. The built-in evaluator is `BuiltinPolicyEngine` in
`src/tether/policy/engine.py`; the file format is in `src/tether/policy/config.py`.

## What a decision is computed from

A decision takes these inputs, and nothing else ([ADR-0009](../adr/0009-policy-engine-current-policy.md),
[ADR-0020](../adr/0020-v1-policy-engine-contract.md)):

| Input | Where it comes from |
|---|---|
| Requester | The subject and roles of the validated token that submitted the run, recorded on the run at submission and never changed |
| Agent | The run's agent, bound by the workspace API key at submission |
| Workspace | The run's workspace |
| Tool metadata | The registered descriptor of the tool's pinned version: its name, version, effect, `risk` and `required_permissions` |
| Call identity | The run, the step ordinal and the call hash |

Model text, the model's stated rationale, raw arguments and tool output are never inputs. A model
that writes "this is approved" or "I am an admin" changes nothing, because nothing it writes
reaches the evaluator. Risk and required permissions belong to the tool's registered version, so a
call cannot make itself low-risk. See [Writing a tool](../development/writing-a-tool.md) for how a
tool declares them.

## How an action is decided

The evaluator checks, in this order, and stops at the first refusal:

1. The run's workspace matches the policy's workspace, the requester has a subject, and the agent
   has an entry under `agent_permissions`. A missing entry is a `deny`.
2. **Permissions.** The requester's permissions are the union of `role_permissions` over the
   requester's roles. The effective set is that union **intersected** with the agent's
   permissions. Every permission in the tool's `required_permissions` must be in the effective
   set. Permissions are `resource:action` strings, matched exactly, with no wildcards.
3. **The tool's rule.** No rule for the tool is a `deny`. A `deny` rule is a `deny`.
4. **Risk.** An `allow` rule on a tool registered as `risk="high"` is a `deny`: a high-risk tool is
   never allowed without a person.
5. Otherwise the rule's decision applies: `allow`, or `require_approval` with the rule's
   `approver_roles`.

The intersection means neither party can widen the other. A requester who may restart services
cannot do so through an agent that may not, and an agent that may restart services cannot do so on
behalf of a requester who may not.

Each decision carries reasons from a fixed vocabulary, recorded in the `POLICY_DECISION` audit
event:

| Reason | Meaning |
|---|---|
| `rule_allow`, `rule_require_approval` | The rule decided |
| `no_matching_rule`, `rule_deny` | No rule for the tool, or a `deny` rule |
| `risk_requires_approval` | An `allow` rule on a high-risk tool |
| `agent_lacks:<permission>`, `requester_lacks:<permission>` | The effective permissions do not cover the tool |
| `context_missing:requester`, `context_missing:agent`, `context_missing:tool_permissions`, `workspace_mismatch` | A trusted input is absent or inconsistent |

A `deny` gives the model a typed `denied` result carrying the reasons. Any later calls in the same
model turn are skipped (each with a `CALL_SKIPPED` event), and the model is called again with the
results. A proposal for a tool that is not offered, or whose arguments do not validate, is not a policy
decision: it is recorded as `CALL_INVALID` and the model gets a typed `invalid` result. Repeated
denials and invalid proposals are bounded: past the configured budgets (`TETHER_MAX_DENIALS`,
`TETHER_MAX_REPAIRS`) the run ends `failed`. See [Run lifecycle](../architecture/run-lifecycle.md).

## The four decision points

The same policy answers four different questions, each at its own point in the flow
([ADR-0022](../adr/0022-verify-operation-policy-mapping.md) added the fourth):

| Decision point | Asked when | Possible answers | Refused means |
|---|---|---|---|
| `evaluate_action` | When a proposal is governed, and again just before the ledger entry is created | `allow`, `deny`, `require_approval` | The call does not run; the model gets a typed result |
| `evaluate_approval_eligibility` | When a person lists, reads or decides an approval | eligible or not | `404` |
| `evaluate_reconciliation_eligibility` | When a person reconciles an `indeterminate` ledger entry | eligible or not | `404` |
| `evaluate_verification` | Before each automatic read-back probe | `allow` or `deny` only | The entry goes to a person |

**Approval eligibility.** The approver must hold a role listed in the tool's `require_approval`
rule and must not be the run's requester. The approval routes first require the core `approver`
role from the token, then ask policy. See [Approvals](approvals.md).

**Reconciliation eligibility.** The reconciler must hold a role in `reconciliation.reconciler_roles`
and must not be the run's requester. An empty list means nobody can reconcile. The route first
requires the core `reconciler` role. See [Unknown outcomes](unknown-outcomes.md).

**Verification.** The probe is treated as its own read-purpose action, never as part of the write's
approval. It is allowed only if the tool's pinned version declares a read-back, the arguments are
exactly the original call's, the current policy version is the one that authorized the write, the
tool's rule is not `deny`, and the requester and agent together hold the read-back's own
`required_permissions`. See [Unknown outcomes](unknown-outcomes.md).

Separation of duties is built into the evaluator: an approver or a reconciler is never the run's
requester, whatever roles they hold. Roles that approve or reconcile grant no tool permissions
unless `role_permissions` says so.

## Policy versions

A workspace's policy version is the SHA-256 of the RFC 8785 canonical JSON of its `policy` section
together with the evaluator's version, `EVALUATOR_VERSION` (currently `builtin-2`). Changing any
rule, permission, role list or expiry produces a new version; so does a new evaluator release, which
is why every upgrade that changes the evaluator version needs `tether bootstrap apply` again.
The file's `agents`, `issuers` and `api_keys` sections are not part of the version.

`tether bootstrap apply` is the only writer of policy versions. It records the new version and makes
it the single active version for the workspace, as the migrator database role, in one transaction
covering the whole file: a file that fails part-way changes nothing. Applying the same file again
changes nothing.
The application role cannot change policy data. Only the version hash is stored: the database does
not keep earlier policy content. See [CLI reference](../reference/cli.md).

### Why every decision is made under the active version

The `api` and `worker` processes load the bootstrap file at startup. During a rollout, some
processes may still hold the old file. If each process decided under whatever it had loaded, two
processes could answer the same question differently, and a revoked permission could still be
used.

So every decision transaction reads the workspace's active version, share-locked, and compares it
with the version the process loaded. If they differ, the process decides nothing:

| Where | What happens with a stale policy |
|---|---|
| Worker, governing or authorizing a call | No decision is recorded; the job is requeued without consuming an attempt and retried later |
| API, deciding or reading approvals, or reconciling | `503` with `policy is reloading`; nothing changes |
| Worker, before a verification probe | Verification ends with reason `policy_stale` and the entry goes to a person |

A recorded decision always names the policy version it was made under, and the audit chain carries
that version on every decision, approval and reconciliation event.

### Re-evaluation before execution

A decision made when the call was proposed is not trusted blindly later. Just before the ledger
entry is created, Tether evaluates current policy again for the same call, with the same trusted
inputs and the tool's pinned descriptor. If policy now denies the call, it is refused even if a
person approved it: a `deny` always overrides an approval. See [Approvals](approvals.md).

## Startup checks

The worker refuses to start if a workspace's policy has an `allow` rule for a tool registered as
`risk="high"`, if a configured workload cannot be loaded, or if any approval expiry exceeds the
configured maximum. `tether bootstrap apply` refuses a file that fails validation and leaves the
database untouched. Validation rejects, among others: two rules for one tool; `approver_roles` on a
rule that is not `require_approval`, or a `require_approval` rule without them;
`approval_expiry_seconds` on a rule that is not `require_approval`; a permission that is not
`resource:action`; and `agent_permissions` for an agent the workspace does not define.

## Example

The development bootstrap file `config/bootstrap/dev.yaml` contains this policy for the workspace
`ops-demo` and its agent `incident-remediator`. The tools come from the incident-remediation
reference workload: `get_service_health` and `get_service_logs` are low-risk reads that need
`service:read`; `restart_service` (needs `service:restart`) and `update_service_config` (needs
`service:configure`) are high-risk writes.

```yaml
policy:
  rules:
    # Low-risk reads: allowed without a person.
    - tool: get_service_health
      decision: allow
    - tool: get_service_logs
      decision: allow
    # High-risk writes: an `allow` here would stop the worker from starting.
    - tool: restart_service
      decision: require_approval
      approver_roles: [approver]      # who may decide; never the run's requester
    - tool: update_service_config
      decision: require_approval
      approver_roles: [approver]
    # Any tool not listed here is denied.
  reconciliation:
    reconciler_roles: [reconciler]    # who may resolve an indeterminate write
  role_permissions:
    # What each requester role grants. `approver` and `reconciler` grant nothing.
    operator: [service:read, service:restart, service:configure]
  agent_permissions:
    # What each agent may do; an agent missing here is denied everything.
    incident-remediator: [service:read, service:restart, service:configure]
```

With this policy:

| Requester roles | Proposal | Decision | Reasons |
|---|---|---|---|
| `operator` | `get_service_health` | `allow` | `rule_allow` |
| `operator` | `restart_service` | `require_approval`, approvers need `approver` | `rule_require_approval` |
| none | `get_service_health` | `deny` | `requester_lacks:service:read` |
| `operator` | a tool with no rule, whose permissions are covered | `deny` | `no_matching_rule` |

A rule may also set its own approval expiry, in seconds. Unset, the deployment default applies
(`TETHER_APPROVAL_TTL_SECONDS`); a value above `TETHER_APPROVAL_MAX_TTL_SECONDS` is refused, never
clamped:

```yaml
    - tool: restart_service
      decision: require_approval
      approver_roles: [approver]
      approval_expiry_seconds: 900
```

## Limits

- The built-in evaluator is a small declarative model, not a general authorization language. It has
  no conditions on arguments, no time windows and no attribute rules; `ActionContext` reserves a
  `trusted_attributes` field for runtime-attested context, which is empty and unused.
- A requester's roles are fixed when the run is submitted. Revoking a role during a run is not
  guaranteed to stop it; exposure is bounded by approval expiry and the run deadline.
- Any policy deployment between a write and its verification probe sends that entry to a person,
  because the probe requires the exact version that authorized the write.
- A refusal at the re-evaluation before execution gives the model a typed result but writes no
  audit event of its own.

## Related

- [Approvals](approvals.md)
- [Unknown outcomes](unknown-outcomes.md)
- [Configuration](../operations/configuration.md)
- [Authentication and authorization](../security/authentication-and-authorization.md)
- [Guarantees](../guarantees.md)

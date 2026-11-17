# Tether documentation

The map of Tether's documentation. Pick the reading path that matches what you need, or find a
page in the table below. Terms are defined in the [glossary](glossary.md).

## Reading paths

**New to Tether.** Read these five pages in order:

1. [Project README](../README.md): what Tether is and why it exists.
2. [Architecture overview](architecture/overview.md): the processes, the store and the path of one
   consequential action.
3. [Guarantees](guarantees.md): what Tether proves, and what it does not claim.
4. [Getting started](development/getting-started.md): a governed run that you approve yourself.
5. [Glossary](glossary.md): the vocabulary every other page uses.

**Evaluating the design.** [Architecture overview](architecture/overview.md), then
[run lifecycle](architecture/run-lifecycle.md), [execution and recovery](architecture/execution-and-recovery.md)
and [data model](architecture/data-model.md). The governance pages explain each control:
[policy](governance/policy.md), [approvals](governance/approvals.md),
[side effects and the execution ledger](governance/side-effects-and-ledger.md),
[unknown outcomes](governance/unknown-outcomes.md) and [audit](governance/audit.md). The
[architecture decision records](adr/README.md) say why each choice was made.

**Running it.** [Getting started](development/getting-started.md) for the development stack.
For a production-shaped deployment: [deployment](operations/deployment.md),
[configuration](operations/configuration.md),
[production configuration](operations/production-configuration.md) and
[model providers](operations/model-providers.md).

**Operating it.** [Runbooks](operations/runbooks.md) first, then
[observability](operations/observability.md), [PostgreSQL recovery](operations/postgres-recovery.md)
and [key lifecycle](operations/key-lifecycle.md). The [CLI reference](reference/cli.md) and the
[HTTP API reference](reference/api.md) hold the exact commands and routes.

**Extending it.** [Writing a tool](development/writing-a-tool.md),
[adding a model provider](development/adding-a-provider.md), [testing](development/testing.md)
and [contributing](../CONTRIBUTING.md).

**Reviewing its security.** [Security model](security/security-model.md), then
[authentication and authorization](security/authentication-and-authorization.md) and
[secrets and data handling](security/secrets-and-data-handling.md). [Audit](governance/audit.md)
covers the integrity of the record, and the [security policy](../SECURITY.md) covers reporting a
vulnerability.

**Checking the evidence.** [Guarantees](guarantees.md) states each claim and its limits;
[validation](validation.md) describes how the claims were tested, the live validation and its
limits; [testing](development/testing.md) shows how to run the suites yourself.

## Every page

| Page | What it covers |
|---|---|
| [README](../README.md) | What Tether is, the problem it solves, a quick start and its known limitations |
| [Glossary](glossary.md) | Definitions of the terms used across the documentation |
| [Guarantees](guarantees.md) | The proven guarantees, behaviour implemented and tested but not yet guaranteed, and what is not claimed |
| [Validation](validation.md) | The automated suites, CI, the chaos harness, the live validation and the limits of that evidence |
| [Architecture overview](architecture/overview.md) | Processes, the PostgreSQL store, layering, and the path of one consequential action |
| [Run lifecycle](architecture/run-lifecycle.md) | Run statuses, the run graph, model turns, bounds, stops, the reconciliation hold and replay |
| [Execution and recovery](architecture/execution-and-recovery.md) | The job queue, leases, heartbeats, backoff, dead-letter, fencing and what a crash does |
| [Data model](architecture/data-model.md) | Tables, roles and grants, workspace scoping, transactions, lock order, checkpoints, migrations |
| [Policy](governance/policy.md) | Deny-by-default decisions, trusted inputs, the four decision points and policy versions |
| [Approvals](governance/approvals.md) | Exact-call binding, the approval lifecycle, eligibility, expiry and the re-check before execution |
| [Side effects and the execution ledger](governance/side-effects-and-ledger.md) | The commits around every send, outcome classes, idempotency keys and re-send rules |
| [Unknown outcomes](governance/unknown-outcomes.md) | `indeterminate` entries, automatic verification and human reconciliation |
| [Audit](governance/audit.md) | Audit events, the per-run HMAC chain, `tether audit verify` and what it detects |
| [Security model](security/security-model.md) | Assets, actors, trust boundaries, threats addressed and what is not defended |
| [Authentication and authorization](security/authentication-and-authorization.md) | Tokens, API keys, audiences, roles, status codes and workspace isolation |
| [Secrets and data handling](security/secrets-and-data-handling.md) | Secret sources, the two key families, masking, and what logs and traces contain |
| [Deployment](operations/deployment.md) | Images, database roles, first-time setup, scaling workers and upgrades |
| [Configuration](operations/configuration.md) | Every environment variable and the bootstrap file |
| [Production configuration](operations/production-configuration.md) | Fail-closed production rules, the startup checklist and migration rollout |
| [Model providers](operations/model-providers.md) | Provider selection, credential handling, the OpenAI adapter and provider retries |
| [Observability](operations/observability.md) | Logs, traces, metrics and what to alert on |
| [Runbooks](operations/runbooks.md) | Procedures for stuck, held or failed runs, dead jobs, approvals and audit checks |
| [PostgreSQL recovery](operations/postgres-recovery.md) | Backup, restore, outages and failover, and what a restore does to guarantees |
| [Key lifecycle](operations/key-lifecycle.md) | The key families and manual rotation by overlap |
| [Getting started](development/getting-started.md) | From a fresh clone to an approved governed run on the development stack |
| [Writing a tool](development/writing-a-tool.md) | Workloads, the tool descriptor, outcome reporting, read-backs and testing a tool |
| [Adding a model provider](development/adding-a-provider.md) | The provider boundary, adapter duties and the provider contract suite |
| [Testing](development/testing.md) | The test suites, the chaos harness, live tests and what CI runs |
| [HTTP API reference](reference/api.md) | Every route, its caller, request, response and status codes |
| [CLI reference](reference/cli.md) | The `tether` commands and the process entry points |
| [Contributing](../CONTRIBUTING.md) | Repository layout, boundaries, checks before a pull request and the decision process |
| [Security policy](../SECURITY.md) | How to report a vulnerability and what is in scope |

## Design history

These are detailed records rather than introductions. Where the design record or an ADR and the
pages above disagree about current behaviour, the pages above describe the code. The guarantee
card stays authoritative for how strong each guarantee is.

- [Architecture decision records](adr/README.md): each significant decision, its alternatives and
  its consequences.
- [Original design record](architecture/tether-v1-architecture.md): the full design. Parts of it
  describe plans rather than the built system.
- [Guarantee card](product/tether-v1-guarantees.md): the authoritative statement of every
  guarantee, with its implementation points, tests and limitations.
- [Test evidence map](testing/strategy.md): which tests prove which guarantee.

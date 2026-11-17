# Contributing to Tether

This page is for anyone who wants to change Tether: how the repository is laid out, the boundaries a change must respect, the checks to run before a pull request, and how decisions are recorded.

**Licence.** No licence has been selected for this repository yet. Until one is, contributions cannot be accepted under stated terms. Please open an issue to discuss a change before investing in a pull request, and expect it to wait for that decision.

## Repository layout

| Path | Contents |
|---|---|
| `src/tether/` | The core: API, worker, run graph, policy, approvals, ledger, audit, storage, CLI. |
| `src/tether/sdk/` | The only module a workload may import. |
| `workloads/` | Reference workloads (`incident_remediation`, `change_control`). Clients of Tether, never part of it. |
| `services/` | The demo target service that the incident workload acts on. |
| `config/bootstrap/` | Example bootstrap files (workspaces, agents, issuers, API keys, policy). |
| `deploy/compose/` | The development stack, the test database and the environment examples. |
| `tools/dev_issuer/` | A development-only token issuer. It is never part of the image. |
| `scripts/` | The demo run, the flake gate and the load campaign. |
| `tests/` | `unit`, `integration`, `authz`, `chaos`, `perf`, `architecture`, `contract`, `support`. |
| `docs/` | Documentation, the decision records in `docs/adr/`, and the guarantee card. |

## Boundaries a change must respect

- **Core, SDK, workloads.** The core never imports a workload. A workload imports only `tether.sdk` from Tether. Both are checked by import contracts and by `tests/architecture/` ([ADR-0002](docs/adr/0002-core-sdk-workload-layering.md)).
- **Nothing workload-specific in `src/tether/`.** No incident, runbook or demo tool names, in code, comments, strings or file names. A test scans for them.
- **A new top-level `tether.<module>`** must be added to the workload `forbidden_modules` list in `pyproject.toml`. Never add `ignore_imports` or allow indirect imports to get a check to pass.
- **Provider code stays behind the adapter.** Model-vendor SDKs and HTTP clients live in the adapter module only; a vendor's name may appear in the adapter, the provider factory and `settings.py`.
- **Fail closed.** Policy, approval, dispatch and production authentication deny when in doubt. A development or test shortcut stays in its isolated module and works only when `TETHER_ENV` is `dev` or `test`.
- **No admin HTTP API.** Platform administration is the bootstrap file and the CLI ([ADR-0014](docs/adr/0014-bootstrap-config-no-admin-api.md)).

The other invariants (one run driver, one status writer, write-ahead audit, masking at a single choke point) are recorded in the ADRs; read the ones that touch your change.

## Set up

You need Python 3.12, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv python install
uv sync --locked
```

[Getting started](docs/development/getting-started.md) takes you through the development stack and a first governed run.

## Before you open a pull request

```bash
uv run poe ci
```

That runs lint and format checks, the strict type check, the import contracts and architecture tests, the unit tests and the provider contract tests. It needs no database. CI runs it on Linux and Windows. Apply formatting with `uv run poe format`, then confirm with `uv run poe lint`.

If your change touches the database, the run graph, workers, authorization or anything that can fail mid-flight, also run the database-backed suites with Docker running and `deploy/compose/.env` created from `deploy/compose/.env.example`:

```bash
uv run poe test-integration
uv run poe test-authz
uv run poe test-contract
uv run poe test-chaos
```

Run one database-backed suite at a time. [Testing](docs/development/testing.md) explains each suite, the chaos harness and the live tests, which are manual and never part of CI.

## Conventions

- **Tests first.** Write the test, watch it fail for the right reason, then write the code. A new guarantee gets a crash or race case in `tests/chaos/`, not only a unit test.
- **Never weaken a test to get green.** No deleted assertions, no `skip`, no `xfail`, no special-casing. If a test looks wrong, say so in the pull request.
- **No new lint, type or import-contract exceptions** to get green.
- **Conventional Commits** for messages: `feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`.
- **One logical change per pull request.** Do not refactor or rename outside the change.
- **No secrets in the repository.** Never commit `.env` files, `tools/dev_issuer/.keys/`, tokens, API keys or private keys. The bootstrap file holds public keys and key hashes only. Use synthetic values in tests and examples.
- **Keep the documents true.** A change to a guarantee updates [the guarantee card](docs/product/tether-v1-guarantees.md) and its tests in the same pull request. A new or changed test updates [the evidence map](docs/testing/strategy.md). A change to behaviour that a page under `docs/` describes updates that page.
- **Describe what exists.** Documentation states what the system does now. Planned or deferred work is labelled as such and is never written as current behaviour.

## How decisions are recorded

Architectural decisions live in `docs/adr/` as numbered Architecture Decision Records ([index](docs/adr/README.md)). An accepted ADR is **never edited** and never overridden by code. To change a decision:

1. Describe the change and the reason in an issue.
2. Once the maintainers agree, write a new ADR from [the template](docs/adr/template.md), with the next number, status `proposed`, naming the ADR it supersedes.
3. A maintainer accepts it. Only then does the conflicting code land.

A pull request that contradicts an accepted ADR will not be merged, however good the code is.

## Where to read more

- [Documentation index](docs/README.md)
- [Architecture overview](docs/architecture/overview.md)
- [Writing a tool](docs/development/writing-a-tool.md) and [adding a provider](docs/development/adding-a-provider.md)
- [Guarantees](docs/guarantees.md) and [validation](docs/validation.md)
- [Security policy](SECURITY.md) for reporting a vulnerability: do not open a public issue for one

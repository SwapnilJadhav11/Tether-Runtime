# ADR-0002: Core / SDK / workload layering with machine-checked import contracts

**Date**: 2026-10-06
**Status**: accepted
**Deciders**: Tether project owner (SwapnilJadhav11); architecture reviewed by ecc:architect
**Architecture source**: AD-02; §24 item 2

## Context

The PRD's domain-independence rule says everything incident-specific lives in a separate workload package and registers only through Tether's public interfaces. Tether core must contain no references to services, incidents, logs, or runbooks, and test T-14 must check this. A second workload (Contract Intelligence) is expected to register later with no core changes. If this boundary is only a convention, it erodes under delivery pressure.

## Decision

1. `tether` (under `src/tether/`) is the runtime. **`tether.sdk` is the only surface workloads may import.** It contains the tool decorator and descriptor fields, the tool outcome type, schema annotations such as `Sensitive`, the policy-rule API, and agent registration.
2. Workloads live outside the runtime package. Bootstrap config names them, and they are discovered through a plugin entry-point mechanism, so **core never imports a workload statically**.
3. An automated import-contract check runs in CI: core must not import workloads, and workloads may import only `tether.sdk`. This check is T-14.
4. If a workload named in bootstrap config fails to load, `api`/`worker` startup fails. A partially loaded registry or policy set never serves requests.
5. The reference workload's demo target service is a separate application. Tether never imports it.

## Alternatives Considered

### A "plugins" folder inside core
- **Pros**: Simple; no packaging step.
- **Cons**: The boundary blurs, and core code can reach workload internals.
- **Why not**: It makes T-14 a code-review convention instead of a machine check.

### Load workloads by file path from config
- **Pros**: No entry-point packaging.
- **Cons**: Nothing controls which surface a workload depends on, and load-time errors are harder to reason about.
- **Why not**: It is weaker than entry points combined with import contracts.

### One repository per workload
- **Pros**: Hard physical boundary.
- **Cons**: Multi-repo overhead inside a ~4-week window.
- **Why not**: A monorepo with enforced contracts gives the same guarantee at much lower cost.

## Rationale

A machine-checked rule cannot quietly regress. The thin SDK becomes the stability boundary that a second workload depends on.

## Consequences

### Positive
- Domain independence is proven on every CI run.
- New workloads register without core changes.
- Everything that defines governance (tools, policies, agents) enters through one reviewed surface.

### Negative
- The SDK surface must be designed up front, and changing it is a compatibility event.
- A workload cannot use a new runtime capability until the SDK exposes it.
- Entry-point discovery adds a packaging step.

### Risks
- The SDK could grow into a back door to core internals. Mitigation: keep it to types and registration APIs.

## Failure and Recovery

- A forbidden import fails CI.
- A missing or broken workload fails process startup closed. Tools or policies are never silently missing at runtime.

## Invariants and Constraints

- §22 invariant 18. Core never reads incident-specific concepts.

## Revisit When

- Third-party or independently deployed workloads need SDK versioning or isolation.
- Untrusted tool code requires sandboxing.
- MCP tool sources are added. These enter through `ToolSource`, not as workload imports.

## References

- PRD: Proposed Solution; Reference Workload / Purpose and boundary (domain-independence rule); Decisions Log (Domain independence, Reference workload); Phase 10; test T-14.
- Architecture: §4 AD-02, proposed directory structure, component responsibility table; §7.

# Security policy

How to report a vulnerability in Tether, and what is in scope. For the threat model and the controls Tether implements, read [the security model](docs/security/security-model.md).

## Reporting a vulnerability

Report it privately through GitHub Private Vulnerability Reporting on this repository: open the **Security** tab and choose **Report a vulnerability**.

The button appears only when a maintainer has enabled private vulnerability reporting in the repository settings (Settings, Code security). If you do not see it, the setting is not enabled. Do not fall back to a public channel: open an issue that asks for a private contact route, without describing the problem.

Do not report a vulnerability as a public issue, a pull request or a discussion. A public report discloses the problem before a fix exists.

Tether is an early project. This policy makes no promise about response or fix times.

## What to include

- The component and the path or route affected (for example `src/tether/identity/jwt.py`, or `POST /v1/approvals/{approval_id}/{action}`).
- The commit you tested, and the environment (`TETHER_ENV` value, model provider, how Tether was started).
- The exact steps to reproduce, with the smallest configuration that shows the problem.
- What you expected, what happened, and the impact you believe it has. A security property that fails is the most useful framing: for example "an approver can decide their own run's approval", or "a tool argument marked `Sensitive` appears in a log line".
- Whether the problem needs a credential, a role, or access to the database or the host.

Use synthetic data. Do not include real credentials, tokens, API keys or private keys in a report. If a reproduction needs a secret, describe where it is used and use a throwaway value.

## In scope

- The Tether core under `src/tether/`: authentication and authorization, policy evaluation, approvals, the execution ledger, dispatch, the audit chain, masking and logging, the job queue and the run driver.
- The workload boundary, `tether.sdk`: a way for a workload to bypass governance, or to make Tether persist or log data that should have been masked.
- The reference workloads' use of that boundary (`workloads/`), where the problem is in how a tool uses the SDK rather than in the target system it calls.
- The production image (`docker build --target prod .`) and its configuration handling.

## Out of scope

- The demo target service (`services/demo_target_service/`), which exists only to give the reference workload something to call.
- Development-only tooling: the development token issuer in `tools/dev_issuer/`, the development Compose stack, and the `dev` image variant.
- The development identity shortcut when `TETHER_ENV` is `dev` or `test`. It exists to make development easy and is refused when `TETHER_ENV` is `prod`. A way to use it in `prod` is in scope.
- Weaknesses in a third-party service a tool calls, in PostgreSQL, in LangGraph, or in a model provider. Report those to their maintainers.
- Documented limits, which the security model lists as not defended:
  - prompt injection detection, a compromised database administrator or key holder, and untrusted tool code;
  - rate limiting and TLS termination, which the deployment provides;
  - replay and revocation of bearer tokens;
  - removal of the end of an audit chain by someone who also rewrites its persisted head;
  - unmasked arguments in job payloads and checkpoints, and fields a tool author did not mark `Sensitive`;
  - OIDC or JWKS discovery, and production service principals, which do not exist.

  A report that shows one of these limits is wider than documented is welcome.

## Licence

No licence has been selected for this repository yet. Reporting a vulnerability does not grant or imply any licence.

## Further reading

- [Security model](docs/security/security-model.md): assets, actors, trust boundaries, and what is and is not defended.
- [Authentication and authorization](docs/security/authentication-and-authorization.md)
- [Secrets and data handling](docs/security/secrets-and-data-handling.md)

"""Load campaign against a running Tether stack, over HTTP, with real credentials.

Submits runs, approves pending approvals as an approver identity, optionally reconciles
``indeterminate`` entries as a reconciler, optionally runs a chaos command every N seconds, waits
for every run to finish, checks correctness and writes a JSON results file. Exits non-zero on any
correctness failure (a non-terminal run, an invalid audit chain, a duplicate effect). Nothing here
is specific to a workload: pass its agent, task message and scenario.

Credentials are read at run time from environment variables (or files named by them) and are never
printed or written to the results file::

    TETHER_CAMPAIGN_API_KEY[_FILE]          the workspace API key (tak_...)
    TETHER_CAMPAIGN_REQUESTER_JWT[_FILE]    requester token (audience: tasks)
    TETHER_CAMPAIGN_APPROVER_JWT[_FILE]     approver token (audience: approvals)
    TETHER_CAMPAIGN_RECONCILER_JWT[_FILE]   reconciler token (ops), with --reconcile-ids-command

Mint dev tokens with ``tools/dev_issuer/mint.py``. Tokens expire: use ``--refresh-command`` (a
command that prints a fresh token, run once per ``--refresh-seconds``) or mint long-lived ones.
``--allow-dev-identity`` (local smoke only, a dev/test stack) uses the dev identity shortcut
instead of credentials.

Scripted campaign (compose stack, ``TETHER_ENV=dev``)::

    uv run poe smoke
    uv run python scripts/load_campaign.py --workspace ops-demo --agent incident-remediator \\
        --runs 50 --concurrency 10 --scenario restart_checkout --allow-dev-identity \\
        --target-counter-url http://localhost:8081/services/checkout --target-counter-field restarts

Real-model campaign: same command against a stack whose provider is OpenAI, with
``--message "<a real task>"`` and no ``--scenario``; expected effects then depend on the model, so
omit the duplicate check or give ``--expect-effects`` only when you know it.

Scale workers with ``docker compose -f deploy/compose/docker-compose.yml up -d --scale worker=N``.
Chaos, for example: ``--chaos-command "docker compose -f deploy/compose/docker-compose.yml
kill worker" --chaos-restart-command "docker compose -f deploy/compose/docker-compose.yml up -d
worker" --chaos-every 20``. The script only runs the commands you give it.

Numbers are what was measured on that stack and machine; they are not operating limits.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shlex
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "abandoned"}


def say(text: str) -> None:
    sys.stderr.write(f"{text}\n")


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def credential(name: str) -> str | None:
    """``TETHER_CAMPAIGN_<name>`` or the contents of the file named by ``..._FILE``."""
    value = os.environ.get(f"TETHER_CAMPAIGN_{name}")
    if value:
        return value.strip()
    path = os.environ.get(f"TETHER_CAMPAIGN_{name}_FILE")
    return Path(path).read_text(encoding="utf-8").strip() if path else None


class Identities:
    """Headers/bodies for each role. Real credentials by default, the dev shortcut on request."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.dev = args.allow_dev_identity
        self.workspace, self.agent = args.workspace, args.agent
        self.refresh = args.refresh_command
        self.refresh_seconds = args.refresh_seconds
        self._tokens: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()
        if not self.dev:
            missing = [
                n
                for n in ("API_KEY", "REQUESTER_JWT", "APPROVER_JWT")
                + (("RECONCILER_JWT",) if args.reconcile_ids_command else ())
                if credential(n) is None
            ]
            if missing:
                sys.exit(f"missing credentials (see --help): {', '.join(missing)}")

    def _token(self, name: str) -> str:
        with self._lock:
            token, fetched = self._tokens.get(name, ("", 0.0))
            stale = self.refresh and time.monotonic() - fetched > self.refresh_seconds
            if not token or stale:
                if self.refresh:
                    out = subprocess.run(  # noqa: S603
                        [*shlex.split(self.refresh), name.lower()],
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=60,
                    )
                    token = out.stdout.strip()
                else:
                    token = credential(name) or ""
                self._tokens[name] = (token, time.monotonic())
            return token

    def submit(self, message: str) -> tuple[dict[str, str], dict[str, Any]]:
        body: dict[str, Any] = {"task_input": {"message": message}}
        if self.dev:
            body["dev_identity"] = {
                "workspace_id": self.workspace,
                "agent_id": self.agent,
                "requester": {"sub": "campaign-requester", "roles": ["operator"]},
            }
            return {}, body
        if self.agent:
            body["agent_id"] = self.agent
        headers = {
            "Authorization": f"Bearer {self._token('REQUESTER_JWT')}",
            "X-API-Key": self._token("API_KEY"),
        }
        return headers, body

    def read(self) -> tuple[dict[str, str], dict[str, str]]:
        if self.dev:
            return {}, {"dev_workspace_id": self.workspace}
        return {
            "Authorization": f"Bearer {self._token('REQUESTER_JWT')}",
            "X-API-Key": self._token("API_KEY"),
        }, {}

    def approve(self, reason: str) -> tuple[dict[str, str], dict[str, Any]]:
        if self.dev:
            identity = {
                "workspace_id": self.workspace,
                "sub": "campaign-approver",
                "roles": ["approver"],
            }
            return {}, {"dev_identity": identity, "reason": reason}
        return {"Authorization": f"Bearer {self._token('APPROVER_JWT')}"}, {"reason": reason}

    def reconcile(self, justification: str) -> tuple[dict[str, str], dict[str, Any]]:
        body = {"outcome": "applied", "justification": justification}
        if self.dev:
            identity = {
                "workspace_id": self.workspace,
                "sub": "campaign-reconciler",
                "roles": ["reconciler"],
            }
            return {}, {**body, "dev_identity": identity}
        return {"Authorization": f"Bearer {self._token('RECONCILER_JWT')}"}, body


class Campaign:
    def __init__(
        self, args: argparse.Namespace, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.args = args
        self.ids = Identities(args)
        self.http = httpx.Client(
            base_url=args.base_url.rstrip("/"), timeout=30, transport=transport
        )
        self.submitted: dict[str, float] = {}  # run id -> monotonic submit time
        self.finished: dict[str, float] = {}  # run id -> monotonic time first seen terminal
        self.views: dict[str, dict[str, Any]] = {}
        self.decided: set[str] = set()
        self.errors: list[str] = []
        self.chaos_events = 0

    # -- steps --

    def submit_one(self, index: int) -> None:
        message = self.args.message
        if self.args.scenario:
            message = f"{message} #scenario={self.args.scenario}"
        headers, body = self.ids.submit(message)
        started = time.monotonic()
        response = self.http.post("/v1/tasks/runs", headers=headers, json=body)
        if response.status_code != 201:
            self.errors.append(f"submit {index}: {response.status_code} {response.text[:200]}")
            return
        self.submitted[response.json()["run_id"]] = started

    def view(self, run_id: str) -> dict[str, Any] | None:
        headers, params = self.ids.read()
        response = self.http.get(f"/v1/tasks/runs/{run_id}", headers=headers, params=params)
        return dict(response.json()) if response.status_code == 200 else None

    def pending_approvals(self, view: dict[str, Any]) -> list[str]:
        """The one place that finds approval ids: replace with the approval list endpoint when it
        exists (the requester's own run view is the only source today)."""
        return [a["id"] for a in view.get("approvals", []) if a["status"] == "pending"]

    def pending_reconciliations(self) -> list[str]:
        """Ledger entry ids to reconcile. The run view does not expose entry ids and no ops
        listing exists yet, so the operator supplies a command that prints them, one per line (for
        example a psql query of indeterminate entries). Replace with the ops endpoint later."""
        if not self.args.reconcile_ids_command:
            return []
        out = subprocess.run(  # noqa: S603
            shlex.split(self.args.reconcile_ids_command),
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]

    def reconcile_round(self) -> None:
        for entry in self.pending_reconciliations():
            if entry in self.decided:
                continue
            self.decided.add(entry)
            headers, body = self.ids.reconcile("load campaign: checked the target")
            url = f"/v1/ops/ledger/{entry}/reconcile"
            response = self.http.post(url, headers=headers, json=body)
            if response.status_code != 200:
                self.errors.append(
                    f"reconcile {entry}: {response.status_code} {response.text[:200]}"
                )

    def act(self, run_id: str, view: dict[str, Any]) -> None:
        for approval in self.pending_approvals(view):
            if approval in self.decided:
                continue
            self.decided.add(approval)
            headers, body = self.ids.approve("load campaign")
            response = self.http.post(
                f"/v1/approvals/{approval}/approve", headers=headers, json=body
            )
            if response.status_code != 200:
                self.errors.append(
                    f"approve {approval}: {response.status_code} {response.text[:200]}"
                )

    def chaos_loop(self, stop: threading.Event) -> None:
        while not stop.wait(self.args.chaos_every):
            for command in (self.args.chaos_command, self.args.chaos_restart_command):
                if command:
                    subprocess.run(shlex.split(command), check=False, timeout=120)  # noqa: S603
            self.chaos_events += 1
            say(f"chaos event {self.chaos_events}")

    def run(self) -> dict[str, Any]:
        begin = time.monotonic()
        with ThreadPoolExecutor(self.args.concurrency) as pool:
            list(pool.map(self.submit_one, range(self.args.runs)))
        say(f"submitted {len(self.submitted)} of {self.args.runs}")
        stop = threading.Event()
        chaos = None
        if self.args.chaos_command and self.args.chaos_every:
            chaos = threading.Thread(target=self.chaos_loop, args=(stop,), daemon=True)
            chaos.start()
        deadline = begin + self.args.timeout
        try:
            while True:
                waiting = [r for r in self.submitted if r not in self.finished]
                if not waiting or time.monotonic() > deadline:
                    break

                def poll(run_id: str) -> None:
                    view = self.view(run_id)
                    if view is None:
                        return
                    self.views[run_id] = view
                    self.act(run_id, view)
                    if view["status"] in TERMINAL:
                        self.finished[run_id] = time.monotonic()

                with ThreadPoolExecutor(self.args.concurrency) as pool:
                    list(pool.map(poll, waiting))
                self.reconcile_round()
                time.sleep(self.args.poll_seconds)  # polling an api, not waiting for an outcome
        finally:
            stop.set()
        wall = time.monotonic() - begin
        return self.report(wall)

    def target_counter(self) -> int | None:
        if not self.args.target_counter_url:
            return None
        body = httpx.get(self.args.target_counter_url, timeout=30).json()
        return (
            int(body[self.args.target_counter_field])
            if self.args.target_counter_field
            else int(body)
        )

    def report(self, wall: float) -> dict[str, Any]:
        statuses = Counter(v["status"] for v in self.views.values())
        stuck = [r for r in self.submitted if r not in self.finished]
        invalid = [r for r, v in self.views.items() if v.get("audit_chain_valid") is not True]
        latencies = [self.finished[r] - self.submitted[r] for r in self.finished]
        failures = [*self.errors]
        if len(self.submitted) != self.args.runs:
            failures.append(f"only {len(self.submitted)} of {self.args.runs} runs were accepted")
        if stuck:
            failures.append(f"{len(stuck)} run(s) not terminal at the timeout: {stuck[:5]}")
        if invalid:
            failures.append(f"{len(invalid)} run(s) with an invalid audit chain: {invalid[:5]}")
        if self.args.expect_status:
            wrong = {s: n for s, n in statuses.items() if s != self.args.expect_status}
            if wrong:
                failures.append(f"statuses other than {self.args.expect_status}: {wrong}")
        counter = self.target_counter()
        if self.args.expect_effects is not None and counter is not None:
            delta = counter - (self.args.target_counter_baseline or 0)
            if delta != self.args.expect_effects:
                failures.append(f"target effects {delta} != expected {self.args.expect_effects}")
        elif self.args.expect_effects is not None:
            failures.append("--expect-effects needs --target-counter-url")
        return {
            "arguments": {
                k: v
                for k, v in vars(self.args).items()
                if "command" not in k and k != "refresh_command"
            },
            "machine": {"platform": platform.platform(), "python": platform.python_version()},
            "submitted": len(self.submitted),
            "statuses": dict(statuses),
            "stuck_runs": len(stuck),
            "invalid_audit_chains": len(invalid),
            "wall_seconds": round(wall, 3),
            "runs_per_second": round(len(self.finished) / wall, 3) if wall else 0,
            "latency_seconds": {
                f"p{int(f * 100)}": percentile(latencies, f) for f in (0.5, 0.95, 0.99)
            },
            "latency_note": "submit to the first poll that saw a terminal status",
            "target_counter": counter,
            "chaos_events": self.chaos_events,
            "correct": not failures,
            "failures": failures,
        }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Load campaign against a running Tether stack.")
    p.add_argument("--base-url", default="http://localhost:8080", help="api base URL")
    p.add_argument("--workspace", required=True, help="workspace id (used by the dev shortcut)")
    p.add_argument("--agent", help="agent id (needed when the API key binds several agents)")
    p.add_argument("--runs", type=int, default=20, help="number of runs to submit")
    p.add_argument("--concurrency", type=int, default=8, help="parallel submit/poll requests")
    p.add_argument("--message", default="load campaign task", help="task message")
    p.add_argument("--scenario", help="scripted-provider scenario (appends #scenario=<name>)")
    p.add_argument("--timeout", type=float, default=600, help="seconds to wait for every run")
    p.add_argument("--poll-seconds", type=float, default=0.5, help="pause between poll rounds")
    p.add_argument(
        "--output", type=Path, default=Path("load-campaign-results.json"), help="results file"
    )
    p.add_argument(
        "--reconcile-ids-command",
        help="command printing ledger entry ids to reconcile as applied, one per line "
        "(the run view does not expose entry ids)",
    )
    p.add_argument(
        "--expect-status", default="succeeded", help="required status of every run ('' to skip)"
    )
    p.add_argument("--target-counter-url", help="URL returning the target's effect counter (JSON)")
    p.add_argument(
        "--target-counter-field", help="JSON field of that counter (default: the whole body)"
    )
    p.add_argument("--target-counter-baseline", type=int, help="counter value before the campaign")
    p.add_argument(
        "--expect-effects", type=int, help="effects the target must show (after baseline)"
    )
    p.add_argument(
        "--chaos-command", help="command run every --chaos-every seconds (e.g. kill a worker)"
    )
    p.add_argument("--chaos-restart-command", help="command run right after the chaos command")
    p.add_argument("--chaos-every", type=float, default=0, help="seconds between chaos events")
    p.add_argument("--refresh-command", help="command that prints a fresh token for a role name")
    p.add_argument("--refresh-seconds", type=float, default=240, help="token refresh interval")
    p.add_argument(
        "--allow-dev-identity",
        action="store_true",
        help="LOCAL SMOKE ONLY: use the dev identity shortcut instead of credentials",
    )
    return p


def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    args = parser().parse_args(argv)
    result = Campaign(args, transport).run()
    args.output.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    say(
        json.dumps(
            {
                k: result[k]
                for k in ("submitted", "statuses", "wall_seconds", "runs_per_second", "correct")
            }
        )
    )
    for failure in result["failures"]:
        say(f"FAIL: {failure}")
    say(f"results written to {args.output}")
    return 0 if result["correct"] else 1


if __name__ == "__main__":
    sys.exit(main())

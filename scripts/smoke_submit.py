"""End-to-end demo against the running compose stack (TETHER_ENV=dev).

    uv run poe smoke                    # build and start the stack
    uv run python scripts/smoke_submit.py

Submits a task as alice (operator), waits for the restart to need approval, approves it as
bob (approver), waits for the run to finish and prints the inspection view. Uses the dev-only
identity shortcut, which the api refuses outside dev and test.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

import httpx

API = "http://localhost:8080"
WORKSPACE = "ops-demo"


def say(text: str) -> None:
    sys.stdout.write(f"{text}\n")


def wait_for(http: httpx.Client, run_id: str, wanted: set[str], timeout: float = 60) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = http.get(f"/v1/tasks/runs/{run_id}", params={"dev_workspace_id": WORKSPACE})
        view.raise_for_status()
        body = view.json()
        if body["status"] in wanted:
            return body
        time.sleep(0.5)
    sys.exit(f"run {run_id} did not reach {sorted(wanted)} in {timeout:.0f}s")


def main() -> None:
    with httpx.Client(base_url=API, timeout=10) as http:
        submitted = http.post(
            "/v1/tasks/runs",
            json={
                "dev_identity": {
                    "workspace_id": WORKSPACE,
                    "agent_id": "incident-remediator",
                    "requester": {"sub": "alice", "roles": ["operator"]},
                },
                "task_input": {"message": "checkout is down #scenario=restart_checkout"},
            },
        )
        submitted.raise_for_status()
        run_id = submitted.json()["run_id"]
        say(f"submitted run {run_id}")

        paused = wait_for(http, run_id, {"awaiting_approval", "failed", "succeeded"})
        if paused["status"] != "awaiting_approval":
            sys.exit(f"unexpected status {paused['status']}: {paused['outcome_summary']}")
        [approval] = [a for a in paused["approvals"] if a["status"] == "pending"]
        say(
            f"awaiting approval {approval['id']}: {approval['tool']} {approval['arguments_masked']}"
        )

        decided = http.post(
            f"/v1/approvals/{approval['id']}/approve",
            json={
                "dev_identity": {"workspace_id": WORKSPACE, "sub": "bob", "roles": ["approver"]},
                "reason": "checkout is unhealthy; restart approved",
            },
        )
        decided.raise_for_status()
        say(f"approved by bob: {decided.json()['status']}")

        final = wait_for(http, run_id, {"succeeded", "failed", "timed_out"})
        say(json.dumps(final, indent=2, default=str))
        if final["status"] != "succeeded" or not final["audit_chain_valid"]:
            sys.exit("the demo run did not complete cleanly")
        say("demo OK: governed, approved, executed, persisted, completed, inspected")


if __name__ == "__main__":
    main()

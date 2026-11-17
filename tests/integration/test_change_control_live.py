"""T-30: the change-control workload against the real GitHub sandbox repository (credentialed).

Skipped unless ``CHANGE_CONTROL_GITHUB_REPO`` and ``CHANGE_CONTROL_GITHUB_TOKEN`` are set, and
unless ``CHANGE_CONTROL_LIVE_CONFIRM=sandbox`` states that the repository is the dedicated
sandbox. Run only with the owner's confirmation of the repository, token scope and cleanup
(OWNER-GATES GATE-3): ``pytest -m live tests/integration/test_change_control_live.py``.

The test creates one issue through a governed, approved run, asserts it exists exactly once by its
idempotency marker, labels and closes it through a second governed run, and in teardown closes
every open issue whose title carries this test's unique tag. Token scope needed: issues
read/write on the sandbox repository only. Cleanup of anything left over: close open issues whose
title starts with ``[tether-live-test`` (no deletion is possible or attempted).
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import yaml
from tests.integration.test_change_control_runs import (
    APPROVER,
    BOOT,
    RecordingProvider,
    _settings,
    approvals,
    chain_valid,
    decide,
    drain,
    ledger,
    rows,
    run_status,
    submit,
)
from workloads.change_control import client as github_client
from workloads.change_control import tools

from tether.cli.bootstrap import apply_bootstrap
from tether.runtime.driver import WorkerRuntime
from tether.testing.scripted import ScriptedProvider

REPO = os.environ.get("CHANGE_CONTROL_GITHUB_REPO", "")
CREDENTIAL = os.environ.get("CHANGE_CONTROL_GITHUB_TOKEN", "")
CONFIRMED = os.environ.get("CHANGE_CONTROL_LIVE_CONFIRM") == "sandbox"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (REPO and CREDENTIAL and CONFIRMED),
        reason="needs CHANGE_CONTROL_GITHUB_REPO, CHANGE_CONTROL_GITHUB_TOKEN and "
        "CHANGE_CONTROL_LIVE_CONFIRM=sandbox",
    ),
]


def _api() -> httpx.Client:
    return httpx.Client(
        base_url=os.environ.get("CHANGE_CONTROL_GITHUB_API_URL", github_client.DEFAULT_API_URL),
        timeout=20.0,
        headers={
            "Authorization": f"Bearer {CREDENTIAL}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": github_client.API_VERSION,
            "User-Agent": "tether-change-control-live-test",
        },
    )


@pytest.fixture
def tag() -> Iterator[str]:
    """A unique title tag; teardown closes every open issue carrying it."""
    value = f"[tether-live-test {uuid.uuid4().hex[:12]}]"
    yield value
    with _api() as http:
        found = http.get(
            f"/repos/{REPO}/issues", params={"state": "open", "labels": tools.CHANGE_LABEL}
        )
        for issue in found.json() if found.status_code == 200 else []:
            if value in issue.get("title", "") and "pull_request" not in issue:
                http.patch(f"/repos/{REPO}/issues/{issue['number']}", json={"state": "closed"})


@pytest_asyncio.fixture
async def live_runtime(seeded: Any, tmp_path: Path, tag: str) -> AsyncIterator[WorkerRuntime]:
    github_client.set_transport(None)  # the real network
    apply_bootstrap(seeded.db.migrator_dsn, BOOT)
    provider = RecordingProvider(ScriptedProvider(tmp_path, env="test"))
    runtime = await WorkerRuntime.open(
        _settings(seeded.db, tmp_path), bootstrap=BOOT, provider=provider
    )
    try:
        yield runtime
    finally:
        await runtime.close()


def _write_scenario(folder: Path, name: str, turns: list[dict[str, Any]]) -> None:
    (folder / f"{name}.yaml").write_text(
        yaml.safe_dump({"name": name, "turns": turns}), encoding="utf-8"
    )


def _issues_with_marker(key: str) -> list[dict[str, Any]]:
    marker = tools.marker_for(key)
    found: list[dict[str, Any]] = []
    with _api() as http:
        for page in range(1, tools.MAX_PAGES + 1):
            response = http.get(
                f"/repos/{REPO}/issues",
                params={
                    "state": "all",
                    "labels": tools.CHANGE_LABEL,
                    "per_page": 100,
                    "page": page,
                },
            )
            response.raise_for_status()
            items = response.json()
            found += [i for i in items if marker in (i.get("body") or "")]
            if 'rel="next"' not in response.headers.get("link", ""):
                break
    return found


def _listing_shows(
    key: str, ready: Callable[[list[dict[str, Any]]], bool], timeout: float = 60.0
) -> list[dict[str, Any]]:
    """Poll the (eventually consistent) listing until ``ready``; return the last listing read."""
    deadline = time.monotonic() + timeout
    found = _issues_with_marker(key)
    while not ready(found) and time.monotonic() < deadline:
        time.sleep(1.0)
        found = _issues_with_marker(key)
    return found


@pytest.mark.asyncio
async def test_t30_a_governed_approved_run_creates_one_real_issue_then_labels_and_closes_it(
    live_runtime: WorkerRuntime, seeded: Any, tmp_path: Path, tag: str
) -> None:
    title = f"{tag} Rotate the deploy key"
    _write_scenario(
        tmp_path,
        "live_create",
        [
            {
                "tool_calls": [
                    {
                        "tool": "create_change_issue",
                        "arguments": {"title": title, "body": "Created by a Tether live test."},
                    }
                ]
            },
            {"final": "created."},
        ],
    )
    run_id = await submit(seeded, tmp_path, "live_create")
    await drain(live_runtime)
    assert run_status(seeded, run_id)[0] == "awaiting_approval"
    [(approval_id, *_)] = approvals(seeded, run_id)
    assert (await decide(seeded, tmp_path, approval_id, "approve", APPROVER)).status_code == 200
    await drain(live_runtime)
    assert run_status(seeded, run_id) == ("succeeded", "created.")
    assert await chain_valid(seeded, tmp_path, run_id)

    [entry] = ledger(seeded, run_id)
    [(key,)] = rows(
        seeded, "SELECT idempotency_key FROM tether.ledger_entries WHERE id = %s", entry[0]
    )
    # GitHub's listing lags a write by seconds: wait for the issue to appear, then let the listing
    # settle before counting, so a duplicate would have had time to show as well.
    _listing_shows(key, lambda found: len(found) >= 1)
    time.sleep(tools.LISTING_SETTLE_SECONDS)
    [issue] = _issues_with_marker(key)  # exactly one issue carries this run's marker
    assert issue["title"] == title

    _write_scenario(
        tmp_path,
        "live_close",
        [
            {
                "tool_calls": [
                    {
                        "tool": "apply_label",
                        "arguments": {"issue_number": issue["number"], "label": "approved"},
                    }
                ]
            },
            {
                "tool_calls": [
                    {"tool": "close_issue", "arguments": {"issue_number": issue["number"]}}
                ]
            },
            {"final": "closed."},
        ],
    )
    close_run = await submit(seeded, tmp_path, "live_close")
    await drain(live_runtime)
    [(close_approval, *_)] = approvals(seeded, close_run)
    await decide(seeded, tmp_path, close_approval, "approve", APPROVER)
    await drain(live_runtime)
    assert run_status(seeded, close_run) == ("succeeded", "closed.")
    [after] = _listing_shows(
        key,
        lambda found: (
            len(found) == 1
            and found[0]["state"] == "closed"
            and "approved" in [label["name"] for label in found[0]["labels"]]
        ),
    )
    assert after["state"] == "closed"
    assert "approved" in [label["name"] for label in after["labels"]]

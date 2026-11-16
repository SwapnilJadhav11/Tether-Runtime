"""Unit-level checks of ``scripts/load_campaign.py`` against a fake api (no stack, no database)."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.perf
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "load_campaign.py"


def campaign() -> ModuleType:
    spec = importlib.util.spec_from_file_location("load_campaign", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["load_campaign"] = module
    spec.loader.exec_module(module)
    return module


class FakeApi:
    """Every run needs one approval, then succeeds; ``chain_ok`` controls the audit chain."""

    def __init__(self, *, chain_ok: bool = True) -> None:
        self.runs: dict[str, str] = {}
        self.headers: list[httpx.Headers] = []
        self.chain_ok = chain_ok

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        path = request.url.path
        if path == "/v1/tasks/runs":
            run_id = f"run-{len(self.runs)}"
            self.runs[run_id] = "awaiting_approval"
            return httpx.Response(201, json={"run_id": run_id, "status": "queued"})
        if match := re.fullmatch(r"/v1/tasks/runs/(.+)", path):
            run_id = match.group(1)
            view: dict[str, Any] = {
                "status": self.runs[run_id],
                "audit_chain_valid": self.chain_ok,
                "approvals": [{"id": f"ap-{run_id}", "status": "pending"}],
            }
            return httpx.Response(200, json=view)
        if match := re.fullmatch(r"/v1/approvals/ap-(.+)/approve", path):
            self.runs[match.group(1)] = "succeeded"
            return httpx.Response(200, json={"status": "approved"})
        return httpx.Response(404)


def run(tmp_path: Path, api: FakeApi, *extra: str) -> tuple[int, dict[str, Any]]:
    out = tmp_path / "results.json"
    code = campaign().main(
        [
            "--workspace",
            "ws",
            "--runs",
            "5",
            "--concurrency",
            "2",
            "--poll-seconds",
            "0.01",
            "--timeout",
            "10",
            "--allow-dev-identity",
            "--output",
            str(out),
            *extra,
        ],
        transport=httpx.MockTransport(api),
    )
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_p03_a_campaign_approves_waits_and_writes_correct_results(tmp_path: Path) -> None:
    code, result = run(tmp_path, FakeApi())
    assert code == 0
    assert result["correct"] is True
    assert result["statuses"] == {"succeeded": 5}
    assert result["stuck_runs"] == 0
    assert result["latency_seconds"]["p50"] is not None


def test_p03_an_invalid_audit_chain_fails_the_campaign(tmp_path: Path) -> None:
    code, result = run(tmp_path, FakeApi(chain_ok=False))
    assert code == 1
    assert result["invalid_audit_chains"] == 5
    assert result["correct"] is False


def test_p03_runs_that_never_finish_fail_the_campaign(tmp_path: Path) -> None:
    original = FakeApi()

    def ignore_approvals(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/approve"):
            return httpx.Response(200, json={})
        return original(request)

    out = tmp_path / "results.json"
    code = campaign().main(
        [
            "--workspace",
            "ws",
            "--runs",
            "2",
            "--poll-seconds",
            "0.01",
            "--timeout",
            "0.5",
            "--allow-dev-identity",
            "--output",
            str(out),
        ],
        transport=httpx.MockTransport(ignore_approvals),
    )
    result = json.loads(out.read_text(encoding="utf-8"))
    assert code == 1
    assert result["stuck_runs"] == 2


def test_p03_without_credentials_or_the_dev_flag_the_script_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("API_KEY", "REQUESTER_JWT", "APPROVER_JWT"):
        monkeypatch.delenv(f"TETHER_CAMPAIGN_{name}", raising=False)
        monkeypatch.delenv(f"TETHER_CAMPAIGN_{name}_FILE", raising=False)
    with pytest.raises(SystemExit, match="missing credentials"):
        campaign().main(["--workspace", "ws"])


def test_p03_real_credentials_are_sent_and_never_written_to_the_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TETHER_CAMPAIGN_API_KEY", "tak_fake_SECRETKEYVALUE")
    monkeypatch.setenv("TETHER_CAMPAIGN_REQUESTER_JWT", "requester.jwt.SECRET")
    monkeypatch.setenv("TETHER_CAMPAIGN_APPROVER_JWT", "approver.jwt.SECRET")
    api = FakeApi()
    out = tmp_path / "results.json"
    code = campaign().main(
        ["--workspace", "ws", "--runs", "2", "--poll-seconds", "0.01", "--output", str(out)],
        transport=httpx.MockTransport(api),
    )
    assert code == 0
    assert any(h.get("x-api-key") == "tak_fake_SECRETKEYVALUE" for h in api.headers)
    assert any(h.get("authorization") == "Bearer approver.jwt.SECRET" for h in api.headers)
    assert "SECRET" not in out.read_text(encoding="utf-8")

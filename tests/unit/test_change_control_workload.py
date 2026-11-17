"""T-31 (unit): the change-control workload against a mocked GitHub.

Descriptor validation, request shapes, outcome mapping (ADR-0007/0019: only positive evidence is
``not_applied``), the idempotency marker round-trip, the verify probe and the configuration guard.
"""

from __future__ import annotations

from collections.abc import Iterator
from importlib.metadata import entry_points

import httpx
import pytest
from pydantic import ValidationError
from tests.support.fake_github import REPO, FakeGitHub
from workloads.change_control import WORKLOAD, client, tools

from tether.sdk import ToolContext, ToolNotApplied, VerificationResult, VerifyContext

CTX = ToolContext(idempotency_key="tth_key_one")
VCTX = VerifyContext(idempotency_key="tth_key_one")
FAKE_CREDENTIAL = "ghp_unit_test_token_value"  # synthetic


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHub]:
    fake = FakeGitHub()
    monkeypatch.setenv("CHANGE_CONTROL_GITHUB_REPO", REPO)
    monkeypatch.setenv("CHANGE_CONTROL_GITHUB_TOKEN", FAKE_CREDENTIAL)
    monkeypatch.delenv("CHANGE_CONTROL_GITHUB_API_URL", raising=False)

    async def no_wait(seconds: float) -> None:  # the fake's listing is consistent
        return None

    monkeypatch.setattr(tools, "_settle", no_wait)
    client.set_transport(fake.transport())
    yield fake
    client.set_transport(None)


def create_args(title: str = "Rotate the deploy key", body: str = "Because.") -> tools.CreateIn:
    return tools.CreateIn(title=title, body=body)


async def run_probe(vctx: VerifyContext = VCTX) -> VerificationResult:
    declaration = tools.create_change_issue.verify
    assert declaration is not None
    return await declaration.probe(create_args(), vctx)


# --- descriptors ------------------------------------------------------------------------------


def test_t31_the_workload_declares_five_tools_with_the_agreed_risk_and_idempotency() -> None:
    by_name = {t.name: t for t in WORKLOAD.tools}
    assert set(by_name) == {
        "get_issue",
        "list_open_change_issues",
        "create_change_issue",
        "apply_label",
        "close_issue",
    }
    assert set(WORKLOAD.agent.tools) == set(by_name)
    assert {n: (t.effect, t.risk) for n, t in by_name.items()} == {
        "get_issue": ("read", "low"),
        "list_open_change_issues": ("read", "low"),
        "create_change_issue": ("write", "high"),
        "apply_label": ("write", "low"),
        "close_issue": ("write", "high"),
    }
    assert by_name["apply_label"].idempotency == "supported"
    assert by_name["close_issue"].idempotency == "supported"
    assert by_name["create_change_issue"].idempotency == "unsupported"
    assert all(t.timeout_seconds < 60 for t in by_name.values())
    permissions = {t.required_permissions for t in by_name.values() if t.effect == "write"}
    assert len(permissions) == 3  # distinct per write tool


def test_t31_only_create_declares_a_verification_and_it_needs_read_permission_only() -> None:
    by_name = {t.name: t for t in WORKLOAD.tools}
    assert by_name["apply_label"].verify is None
    verify = by_name["create_change_issue"].verify
    assert verify is not None
    assert verify.required_permissions == ("issue:read",)


def test_t31_no_tool_takes_a_repository_owner_url_or_token_argument() -> None:
    forbidden = {"repo", "repository", "owner", "url", "token", "base_url", "api_url"}
    for tool in WORKLOAD.tools:
        assert not forbidden & set(tool.input_model.model_fields), tool.name


def test_t31_the_workload_loads_by_entry_point_name() -> None:
    [point] = [e for e in entry_points(group="tether.workloads") if e.name == "change_control"]
    assert point.load() is WORKLOAD


def test_t31_inputs_reject_labels_outside_the_allowlist_and_forged_markers() -> None:
    with pytest.raises(ValidationError):
        tools.LabelIn(issue_number=1, label="ship-it")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        tools.IssueRef(issue_number=0)
    with pytest.raises(ValidationError):
        create_args(body="hello <!-- tether:idempotency-key=tth_x -->")
    with pytest.raises(ValidationError):
        tools.CreateIn(title="x", body="y", repo="other/repo")  # type: ignore[call-arg]


# --- configuration guard ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["CHANGE_CONTROL_GITHUB_REPO", "CHANGE_CONTROL_GITHUB_TOKEN"])
async def test_t31_without_configuration_no_request_leaves_the_process(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.delenv(missing)
    for tool, args in (
        (tools.get_issue, tools.IssueRef(issue_number=1)),
        (tools.list_open_change_issues, tools.NoArgs()),
        (tools.create_change_issue, create_args()),
        (tools.apply_label, tools.LabelIn(issue_number=1, label="approved")),
        (tools.close_issue, tools.IssueRef(issue_number=1)),
    ):
        with pytest.raises(client.ConfigurationError):
            await tool.handler(args, CTX)
    assert github.seen == []


@pytest.mark.asyncio
async def test_t31_a_malformed_repository_value_is_a_configuration_error(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHANGE_CONTROL_GITHUB_REPO", "../../orgs/x")
    with pytest.raises(client.ConfigurationError):
        await tools.get_issue.handler(tools.IssueRef(issue_number=1), CTX)
    assert github.seen == []


def test_t31_a_configuration_error_is_positive_evidence_that_nothing_was_sent() -> None:
    assert issubclass(client.ConfigurationError, ToolNotApplied)


# --- request shapes ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t31_create_sends_one_json_request_with_the_marker_and_the_change_label(
    github: FakeGitHub,
) -> None:
    out = await tools.create_change_issue.handler(
        create_args(title="Title with /slashes ?q=1", body="Body\nwith lines"), CTX
    )
    [sent] = github.seen
    assert (sent.method, sent.path) == ("POST", f"/repos/{REPO}/issues")
    assert sent.query == {}
    assert sent.body["title"] == "Title with /slashes ?q=1"
    assert sent.body["body"].startswith("Body\nwith lines")
    assert tools.marker_for("tth_key_one") in sent.body["body"]
    assert sent.body["labels"] == [tools.CHANGE_LABEL]
    assert sent.headers["authorization"] == f"Bearer {FAKE_CREDENTIAL}"
    assert sent.headers["accept"] == "application/vnd.github+json"
    assert sent.headers["x-github-api-version"] == tools.API_VERSION
    assert out.number == 1
    assert out.state == "open"
    assert FAKE_CREDENTIAL not in out.model_dump_json()


@pytest.mark.asyncio
async def test_t31_label_and_close_use_fixed_paths_and_json_bodies(github: FakeGitHub) -> None:
    number = github.add_issue("Existing", labels=(tools.CHANGE_LABEL,))
    labelled = await tools.apply_label.handler(
        tools.LabelIn(issue_number=number, label="approved"), CTX
    )
    closed = await tools.close_issue.handler(tools.IssueRef(issue_number=number), CTX)
    label_req, close_req = github.seen
    assert (label_req.method, label_req.path, label_req.body) == (
        "POST",
        f"/repos/{REPO}/issues/{number}/labels",
        {"labels": ["approved"]},
    )
    assert (close_req.method, close_req.path) == ("PATCH", f"/repos/{REPO}/issues/{number}")
    assert close_req.body["state"] == "closed"
    assert "approved" in labelled.labels
    assert closed.state == "closed"


@pytest.mark.asyncio
async def test_t31_reads_return_state_title_and_labels_and_skip_pull_requests(
    github: FakeGitHub,
) -> None:
    mine = github.add_issue("Change A", labels=(tools.CHANGE_LABEL, "approved"))
    github.add_issue("Not a change", labels=("bug",))
    github.add_issue("A PR", labels=(tools.CHANGE_LABEL,), pull_request=True)
    github.add_issue("Closed change", labels=(tools.CHANGE_LABEL,), state="closed")
    one = await tools.get_issue.handler(tools.IssueRef(issue_number=mine), CTX)
    assert (one.number, one.title, one.state, one.labels) == (
        mine,
        "Change A",
        "open",
        [tools.CHANGE_LABEL, "approved"],
    )
    listed = await tools.list_open_change_issues.handler(tools.NoArgs(), CTX)
    assert [i.number for i in listed.issues] == [mine]
    assert listed.truncated is False
    listing = github.seen[-1]
    assert listing.query["state"] == "open"
    assert listing.query["labels"] == tools.CHANGE_LABEL


@pytest.mark.asyncio
async def test_t31_the_api_base_url_is_overridable_by_configuration(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host)
        return httpx.Response(404, json={})

    client.set_transport(httpx.MockTransport(handler))
    monkeypatch.setenv("CHANGE_CONTROL_GITHUB_API_URL", "http://ghe.internal.test/api/v3")
    with pytest.raises(ToolNotApplied):
        await tools.get_issue.handler(tools.IssueRef(issue_number=3), CTX)
    assert seen_hosts == ["ghe.internal.test"]


# --- outcome mapping --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["404", "410", "422"])
@pytest.mark.parametrize("kind", ["create", "label", "close"])
async def test_t31_a_rejection_is_positive_evidence_of_not_applied(
    github: FakeGitHub, status: str, kind: str
) -> None:
    number = github.add_issue("x")
    github.script[:] = [
        {
            "create": ("POST", "issues", status),
            "label": ("POST", "labels", status),
            "close": ("PATCH", "issue", status),
        }[kind]
    ]
    calls = {
        "create": lambda: tools.create_change_issue.handler(create_args(), CTX),
        "label": lambda: tools.apply_label.handler(
            tools.LabelIn(issue_number=number, label="approved"), CTX
        ),
        "close": lambda: tools.close_issue.handler(tools.IssueRef(issue_number=number), CTX),
    }
    with pytest.raises(ToolNotApplied):
        await calls[kind]()
    assert len(github.writes()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["500", "502", "503", "403", "429", "401"])
async def test_t31_server_errors_and_rate_limits_are_unknown_not_not_applied(
    github: FakeGitHub, status: str
) -> None:
    github.script[:] = [("POST", "issues", status)]
    with pytest.raises(httpx.HTTPStatusError):
        await tools.create_change_issue.handler(create_args(), CTX)


@pytest.mark.asyncio
async def test_t31_a_timeout_after_the_request_was_sent_raises_unknown(
    github: FakeGitHub,
) -> None:
    github.script[:] = [("POST", "issues", "timeout_after")]
    with pytest.raises(httpx.TimeoutException):
        await tools.create_change_issue.handler(create_args(), CTX)
    assert len(github.writes()) == 1  # the handler never retries a create


@pytest.mark.asyncio
async def test_t31_an_unexpected_success_status_is_unknown(github: FakeGitHub) -> None:
    github.script[:] = [("POST", "issues", "202")]
    with pytest.raises(httpx.HTTPStatusError):
        await tools.create_change_issue.handler(create_args(), CTX)


# --- marker and verification ------------------------------------------------------------------


def test_t31_the_marker_is_hidden_and_round_trips_the_idempotency_key() -> None:
    marker = tools.marker_for("tth_abc123")
    assert marker.startswith("<!--")
    assert marker.endswith("-->")
    assert tools.marker_key(f"Visible text\n\n{marker}") == "tth_abc123"
    assert tools.marker_key("no marker here") is None
    assert tools.marker_for("tth_abc123") != tools.marker_for("tth_abc124")


@pytest.mark.asyncio
async def test_t31_verify_confirms_applied_when_the_marker_is_present(github: FakeGitHub) -> None:
    await tools.create_change_issue.handler(create_args(), CTX)
    github.seen.clear()
    result = await run_probe()
    assert result.kind == "confirmed_applied"
    assert result.evidence.issue_number == 1  # type: ignore[attr-defined]
    assert all(s.method == "GET" for s in github.seen)  # safe reads only


@pytest.mark.asyncio
async def test_t31_verify_confirms_not_applied_only_from_a_complete_listing(
    github: FakeGitHub,
) -> None:
    github.add_issue("Other", body=tools.marker_for("tth_other"), labels=(tools.CHANGE_LABEL,))
    result = await run_probe()
    assert result.kind == "confirmed_not_applied"
    assert github.writes() == []


@pytest.mark.asyncio
async def test_t31_verify_does_not_trust_a_listing_that_has_not_caught_up_with_the_write(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub's issue listing lags a create by seconds (measured on the sandbox, 2026-11-17).
    An issue missing from the first complete listing is not yet evidence of anything."""
    waited: list[float] = []

    async def listing_catches_up(seconds: float) -> None:
        waited.append(seconds)
        github.add_issue(
            "Target", body=tools.marker_for("tth_key_one"), labels=(tools.CHANGE_LABEL,)
        )

    monkeypatch.setattr(tools, "_settle", listing_catches_up)
    result = await run_probe()
    assert result.kind == "confirmed_applied"
    assert waited == [tools.LISTING_SETTLE_SECONDS]
    assert tools.LISTING_SETTLE_SECONDS >= 15.0  # the measured lag was up to 8 s
    assert all(s.method == "GET" for s in github.seen)


@pytest.mark.asyncio
async def test_t31_verify_confirms_not_applied_only_after_a_second_listing_past_the_settle_time(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    waited: list[float] = []

    async def record(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr(tools, "_settle", record)
    result = await run_probe()
    assert result.kind == "confirmed_not_applied"
    assert waited == [tools.LISTING_SETTLE_SECONDS]  # exactly one pause
    assert len(github.seen) == 2  # two complete listings, one either side of the pause
    assert result.evidence.pages_scanned == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_t31_verify_is_inconclusive_when_the_second_listing_fails(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def then_github_fails(seconds: float) -> None:
        github.script[:] = [("GET", "issues", "500")]

    monkeypatch.setattr(tools, "_settle", then_github_fails)
    result = await run_probe()
    assert result.kind == "inconclusive"


@pytest.mark.asyncio
async def test_t31_verify_finds_the_marker_on_a_later_page_and_in_closed_issues(
    github: FakeGitHub,
) -> None:
    github.page_size = 2
    for index in range(3):
        github.add_issue(f"filler {index}", labels=(tools.CHANGE_LABEL,))
    github.add_issue(
        "Target", body=tools.marker_for("tth_key_one"), labels=(tools.CHANGE_LABEL,), state="closed"
    )
    result = await run_probe()
    assert result.kind == "confirmed_applied"
    assert github.seen[0].query["state"] == "all"
    assert len(github.seen) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["500", "403", "timeout_before"])
async def test_t31_verify_is_inconclusive_on_error(github: FakeGitHub, action: str) -> None:
    github.script[:] = [("GET", "issues", action)]
    result = await run_probe()
    assert result.kind == "inconclusive"


@pytest.mark.asyncio
async def test_t31_verify_is_inconclusive_when_the_listing_is_not_complete(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    github.page_size = 1
    for index in range(4):
        github.add_issue(f"filler {index}", labels=(tools.CHANGE_LABEL,))
    monkeypatch.setattr(tools, "MAX_PAGES", 2)
    result = await run_probe()
    assert result.kind == "inconclusive"


@pytest.mark.asyncio
async def test_t31_verify_is_inconclusive_without_configuration(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHANGE_CONTROL_GITHUB_REPO")
    result = await run_probe()
    assert result.kind == "inconclusive"
    assert github.seen == []

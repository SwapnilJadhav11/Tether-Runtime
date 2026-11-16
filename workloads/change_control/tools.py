"""Change-control tools against one GitHub sandbox repository.

Outcome honesty (ADR-0007, ADR-0019): a ``404``, ``410`` or ``422`` is GitHub rejecting the
request, which is positive evidence nothing was applied -> ``ToolNotApplied``. A timeout, a
connection error, a ``5xx``, a ``403``/``429`` rate limit, a ``401`` or any unexpected status is
not evidence either way -> the error propagates and Tether classifies the outcome ``unknown``.

Idempotency: ``apply_label`` and ``close_issue`` are naturally idempotent on GitHub. GitHub has no
idempotency key, so ``create_change_issue`` is declared ``unsupported``: Tether sends it at most
once, and an unknown outcome becomes ``indeterminate`` for a human to reconcile. The hidden marker
carrying the idempotency key makes that decidable by a safe read (the ``verify`` probe).

Issue titles, bodies and labels in a sandbox are not sensitive, so no field is ``Sensitive``.
Model-supplied text is only ever sent as JSON, never interpolated into a URL or header.
"""

from __future__ import annotations

import re
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tether.sdk import (
    ToolContext,
    ToolDescriptor,
    ToolNotApplied,
    VerificationResult,
    VerifyContext,
    VerifyDeclaration,
)
from workloads.change_control.client import API_VERSION, ConfigurationError, open_target

__all__ = ["API_VERSION", "ConfigurationError"]

CHANGE_LABEL = "change-control"
Label = Literal["change-control", "needs-review", "approved", "blocked"]
# Pages (of 100) a verification probe may read before it reports ``inconclusive``.
MAX_PAGES = 10
_MARKER_PREFIX = "<!-- tether:idempotency-key="
_MARKER = re.compile(r"<!-- tether:idempotency-key=([A-Za-z0-9_-]+) -->")
_NOT_APPLIED = (404, 410, 422)


def marker_for(idempotency_key: str) -> str:
    """A hidden HTML comment: invisible in the rendered issue, readable back through the API."""
    return f"{_MARKER_PREFIX}{idempotency_key} -->"


def marker_key(body: str | None) -> str | None:
    found = _MARKER.search(body or "")
    return found.group(1) if found else None


# --- models -----------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(_Strict):
    pass


class IssueRef(_Strict):
    issue_number: int = Field(ge=1, le=10_000_000, description="Issue number")


class LabelIn(_Strict):
    issue_number: int = Field(ge=1, le=10_000_000, description="Issue number")
    label: Label = Field(description="One label from the fixed allowlist")


class CreateIn(_Strict):
    title: str = Field(min_length=1, max_length=256, description="Issue title")
    body: str = Field(max_length=20_000, description="Issue body")

    @field_validator("body")
    @classmethod
    def _no_forged_marker(cls, value: str) -> str:
        if _MARKER_PREFIX in value:
            raise ValueError("the body must not contain a Tether idempotency marker")
        return value


class IssueView(BaseModel):
    number: int
    title: str
    state: str
    labels: list[str]
    url: str | None = None


class IssueList(BaseModel):
    issues: list[IssueView]
    truncated: bool


class VerifyEvidence(BaseModel):
    issue_number: int | None = None
    pages_scanned: int = 0
    reason: str = ""


def _view(data: dict[str, object]) -> IssueView:
    raw_labels = data.get("labels") or []
    names = [
        str(label["name"]) if isinstance(label, dict) else str(label)
        for label in raw_labels  # type: ignore[attr-defined]
    ]
    url = data.get("html_url")
    return IssueView(
        number=int(data["number"]),  # type: ignore[call-overload]
        title=str(data.get("title", "")),
        state=str(data.get("state", "")),
        labels=names,
        url=str(url) if url else None,
    )


def _check(response: httpx.Response, expected: int) -> None:
    if response.status_code in _NOT_APPLIED:
        raise ToolNotApplied(f"github rejected the request ({response.status_code})")
    response.raise_for_status()
    if response.status_code != expected:
        # A success status we did not expect says nothing certain about the effect.
        raise httpx.HTTPStatusError(
            f"unexpected status {response.status_code}",
            request=response.request,
            response=response,
        )


# --- handlers ---------------------------------------------------------------------------------


async def _get_issue(args: IssueRef, ctx: ToolContext) -> IssueView:
    target = open_target()
    async with target.client as http:
        response = await http.get(f"/repos/{target.repo}/issues/{args.issue_number}")
    _check(response, 200)
    return _view(response.json())


async def _list_open_change_issues(args: NoArgs, ctx: ToolContext) -> IssueList:
    target = open_target()
    async with target.client as http:
        response = await http.get(
            f"/repos/{target.repo}/issues",
            params={"state": "open", "labels": CHANGE_LABEL, "per_page": 100},
        )
    _check(response, 200)
    issues = [_view(item) for item in response.json() if "pull_request" not in item]
    return IssueList(issues=issues, truncated='rel="next"' in response.headers.get("link", ""))


async def _create_change_issue(args: CreateIn, ctx: ToolContext) -> IssueView:
    target = open_target()
    body = f"{args.body}\n\n{marker_for(ctx.idempotency_key)}"
    async with target.client as http:
        response = await http.post(
            f"/repos/{target.repo}/issues",
            json={"title": args.title, "body": body, "labels": [CHANGE_LABEL]},
        )
    _check(response, 201)
    return _view(response.json())


async def _apply_label(args: LabelIn, ctx: ToolContext) -> IssueView:
    target = open_target()
    async with target.client as http:
        response = await http.post(
            f"/repos/{target.repo}/issues/{args.issue_number}/labels",
            json={"labels": [args.label]},
        )
    _check(response, 200)
    # GitHub answers with the issue's full label list; no second request that could fail.
    names = [str(item.get("name")) for item in response.json() if isinstance(item, dict)]
    return IssueView(number=args.issue_number, title="", state="", labels=names)


async def _close_issue(args: IssueRef, ctx: ToolContext) -> IssueView:
    target = open_target()
    async with target.client as http:
        response = await http.patch(
            f"/repos/{target.repo}/issues/{args.issue_number}",
            json={"state": "closed", "state_reason": "completed"},
        )
    _check(response, 200)
    return _view(response.json())


async def _verify_create(args: CreateIn, ctx: VerifyContext) -> VerificationResult:
    """Safe reads only: list issues (not search, which is eventually consistent) for the marker."""
    try:
        target = open_target()
    except ConfigurationError as error:
        return VerificationResult("inconclusive", VerifyEvidence(reason=str(error)))
    wanted = marker_for(ctx.idempotency_key)
    scanned = 0
    try:
        async with target.client as http:
            for page in range(1, MAX_PAGES + 1):
                response = await http.get(
                    f"/repos/{target.repo}/issues",
                    params={
                        "state": "all",
                        "labels": CHANGE_LABEL,
                        "per_page": 100,
                        "page": page,
                    },
                )
                if response.status_code != 200:
                    return VerificationResult(
                        "inconclusive",
                        VerifyEvidence(
                            pages_scanned=scanned, reason=f"status {response.status_code}"
                        ),
                    )
                scanned += 1
                for item in response.json():
                    if "pull_request" not in item and wanted in (item.get("body") or ""):
                        return VerificationResult(
                            "confirmed_applied",
                            VerifyEvidence(issue_number=int(item["number"]), pages_scanned=scanned),
                        )
                if 'rel="next"' not in response.headers.get("link", ""):
                    return VerificationResult(
                        "confirmed_not_applied", VerifyEvidence(pages_scanned=scanned)
                    )
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
        return VerificationResult(
            "inconclusive",
            VerifyEvidence(pages_scanned=scanned, reason=type(error).__name__),
        )
    return VerificationResult(
        "inconclusive", VerifyEvidence(pages_scanned=scanned, reason="listing not complete")
    )


# --- descriptors ------------------------------------------------------------------------------

get_issue = ToolDescriptor(
    name="get_issue",
    version="1.0.0",
    description="Read the state, title and labels of one issue.",
    input_model=IssueRef,
    output_model=IssueView,
    effect="read",
    handler=_get_issue,
    risk="low",
    required_permissions=("issue:read",),
    timeout_seconds=20.0,
)

list_open_change_issues = ToolDescriptor(
    name="list_open_change_issues",
    version="1.0.0",
    description="List open issues that carry the change-control label.",
    input_model=NoArgs,
    output_model=IssueList,
    effect="read",
    handler=_list_open_change_issues,
    risk="low",
    required_permissions=("issue:read",),
    timeout_seconds=20.0,
)

create_change_issue = ToolDescriptor(
    name="create_change_issue",
    version="1.0.0",
    description="Create a change-control issue with a title and body. Requires approval.",
    input_model=CreateIn,
    output_model=IssueView,
    effect="write",
    idempotency="unsupported",
    handler=_create_change_issue,
    risk="high",
    required_permissions=("issue:create",),
    timeout_seconds=20.0,
    verify=VerifyDeclaration(
        probe=_verify_create, evidence_model=VerifyEvidence, required_permissions=("issue:read",)
    ),
)

apply_label = ToolDescriptor(
    name="apply_label",
    version="1.0.0",
    description="Apply one label from a fixed allowlist to one issue.",
    input_model=LabelIn,
    output_model=IssueView,
    effect="write",
    idempotency="supported",
    handler=_apply_label,
    risk="low",
    required_permissions=("issue:label",),
    timeout_seconds=20.0,
)

close_issue = ToolDescriptor(
    name="close_issue",
    version="1.0.0",
    description="Close one issue. Requires approval.",
    input_model=IssueRef,
    output_model=IssueView,
    effect="write",
    idempotency="supported",
    handler=_close_issue,
    risk="high",
    required_permissions=("issue:close",),
    timeout_seconds=20.0,
)

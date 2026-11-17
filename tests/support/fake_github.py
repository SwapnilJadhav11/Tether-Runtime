"""An in-process fake of the slice of the GitHub REST API the change-control workload uses.

Requests are served by an ``httpx.MockTransport``. Every request is recorded (method, path,
JSON body, headers) so tests can count what left the process. ``script`` makes the next matching
requests misbehave: ``timeout_before`` (nothing applied, no response), ``timeout_after`` (applied,
then no response), or an HTTP status such as ``500`` or ``422`` (nothing applied).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

REPO = "sandbox-owner/sandbox-repo"
_ISSUES = re.compile(rf"^/repos/{REPO}/issues$")
_ISSUE = re.compile(rf"^/repos/{REPO}/issues/(\d+)$")
_LABELS = re.compile(rf"^/repos/{REPO}/issues/(\d+)/labels$")


@dataclass
class Seen:
    method: str
    path: str
    query: dict[str, str]
    body: Any
    headers: dict[str, str]


@dataclass
class FakeGitHub:
    issues: dict[int, dict[str, Any]] = field(default_factory=dict)
    seen: list[Seen] = field(default_factory=list)
    # Each entry is (method, path_kind, action); the first match is consumed.
    script: list[tuple[str, str, str]] = field(default_factory=list)
    page_size: int = 100
    _next: int = 1

    def add_issue(
        self,
        title: str,
        body: str = "",
        labels: tuple[str, ...] = (),
        state: str = "open",
        pull_request: bool = False,
    ) -> int:
        number = self._next
        self._next += 1
        issue: dict[str, Any] = {
            "number": number,
            "title": title,
            "body": body,
            "state": state,
            "labels": [{"name": name} for name in labels],
            "html_url": f"https://github.test/{REPO}/issues/{number}",
        }
        if pull_request:
            issue["pull_request"] = {}
        self.issues[number] = issue
        return number

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def writes(self) -> list[Seen]:
        return [s for s in self.seen if s.method != "GET"]

    def _kind(self, path: str) -> str:
        if _LABELS.match(path):
            return "labels"
        if _ISSUE.match(path):
            return "issue"
        return "issues"

    def _scripted(self, method: str, kind: str) -> str | None:
        for index, (m, k, action) in enumerate(self.script):
            if m == method and k == kind:
                del self.script[index]
                return action
        return None

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        raw = request.content
        body = json.loads(raw) if raw else None
        self.seen.append(
            Seen(
                request.method,
                path,
                dict(request.url.params),
                body,
                {k.lower(): v for k, v in request.headers.items()},
            )
        )
        action = self._scripted(request.method, self._kind(path))
        if action == "timeout_before":
            raise httpx.ReadTimeout("no response", request=request)
        if action is not None and action.isdigit():
            return httpx.Response(int(action), json={"message": "scripted"})
        response = self._serve(request, body)
        if action == "timeout_after":
            raise httpx.ReadTimeout("no response", request=request)
        return response

    def _serve(self, request: httpx.Request, body: Any) -> httpx.Response:
        path, method = request.url.path, request.method
        if (match := _LABELS.match(path)) and method == "POST":
            issue = self.issues.get(int(match.group(1)))
            if issue is None:
                return httpx.Response(404, json={"message": "Not Found"})
            names = {label["name"] for label in issue["labels"]}
            for name in body["labels"]:
                if name not in names:
                    issue["labels"].append({"name": name})
            return httpx.Response(200, json=issue["labels"])
        if match := _ISSUE.match(path):
            issue = self.issues.get(int(match.group(1)))
            if issue is None:
                return httpx.Response(404, json={"message": "Not Found"})
            if method == "GET":
                return httpx.Response(200, json=issue)
            if method == "PATCH":
                issue["state"] = body["state"]
                return httpx.Response(200, json=issue)
        if _ISSUES.match(path):
            if method == "POST":
                if not str(body.get("title", "")).strip():
                    return httpx.Response(422, json={"message": "Validation Failed"})
                number = self.add_issue(
                    body["title"], body.get("body", ""), tuple(body.get("labels", ()))
                )
                return httpx.Response(201, json=self.issues[number])
            if method == "GET":
                return self._list(request)
        return httpx.Response(404, json={"message": "Not Found"})

    def _list(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        state = params.get("state", "open")
        wanted = [name for name in params.get("labels", "").split(",") if name]
        found = [
            issue
            for issue in self.issues.values()
            if (state == "all" or issue["state"] == state)
            and all(any(label["name"] == w for label in issue["labels"]) for w in wanted)
        ]
        page = int(params.get("page", "1"))
        size = min(int(params.get("per_page", "30")), self.page_size)
        chunk = found[(page - 1) * size : page * size]
        headers = {}
        if page * size < len(found):
            next_url = request.url.copy_set_param("page", page + 1)
            headers["Link"] = f'<{next_url}>; rel="next"'
        return httpx.Response(200, json=chunk, headers=headers)

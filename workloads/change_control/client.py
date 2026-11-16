"""HTTP client for the GitHub sandbox repository. The target is configuration, never model input.

Environment: ``CHANGE_CONTROL_GITHUB_REPO`` (``owner/name``), ``CHANGE_CONTROL_GITHUB_TOKEN``
and, for tests or GitHub Enterprise, ``CHANGE_CONTROL_GITHUB_API_URL``. The token is only ever
placed in the ``Authorization`` header; it is never logged, returned or stored.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import httpx

from tether.sdk import ToolNotApplied

API_VERSION = "2022-11-28"
DEFAULT_API_URL = "https://api.github.com"
TIMEOUT_SECONDS = 10.0
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")

_transport: httpx.AsyncBaseTransport | None = None


class ConfigurationError(ToolNotApplied):
    """The workload is not configured. Raised before any request, so nothing was sent."""


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Tests route requests to an in-process fake instead of the network."""
    global _transport
    _transport = transport


@dataclass(frozen=True)
class Target:
    client: httpx.AsyncClient
    repo: str


def open_target() -> Target:
    """Build the client for the configured repository, or raise ``ConfigurationError``."""
    repo = os.environ.get("CHANGE_CONTROL_GITHUB_REPO", "").strip()
    if not repo:
        raise ConfigurationError("CHANGE_CONTROL_GITHUB_REPO is not set")
    if not _REPO.fullmatch(repo) or ".." in repo:
        raise ConfigurationError("CHANGE_CONTROL_GITHUB_REPO must look like owner/name")
    token = os.environ.get("CHANGE_CONTROL_GITHUB_TOKEN", "").strip()
    if not token:
        raise ConfigurationError("CHANGE_CONTROL_GITHUB_TOKEN is not set")
    base_url = os.environ.get("CHANGE_CONTROL_GITHUB_API_URL", DEFAULT_API_URL).rstrip("/")
    http = httpx.AsyncClient(
        base_url=base_url,
        transport=_transport,
        timeout=TIMEOUT_SECONDS,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "tether-change-control",
        },
    )
    return Target(client=http, repo=repo)

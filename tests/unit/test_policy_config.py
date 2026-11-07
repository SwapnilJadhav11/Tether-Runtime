"""MVP M1: bootstrap file model and policy version hashing (ADR-0009, ADR-0014)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tether.policy.config import BootstrapConfigError, BootstrapFile, load_bootstrap

BASE = """
workspaces:
  - id: ws-one
    agents:
      - id: agent-one
        workload: probe
    policy:
      rules:
        - tool: read_thing
          decision: allow
        - tool: write_thing
          decision: require_approval
          approver_roles: [oncall]
"""


def _load(tmp_path: Path, text: str) -> BootstrapFile:
    path = tmp_path / "bootstrap.yaml"
    path.write_text(text, encoding="utf-8")
    return load_bootstrap(path)


def test_policy_version_is_a_stable_content_hash(tmp_path: Path) -> None:
    first = _load(tmp_path, BASE).workspaces[0].policy_version
    second = _load(tmp_path, BASE).workspaces[0].policy_version
    assert first == second
    assert len(first) == 64


def test_policy_version_changes_when_a_rule_changes(tmp_path: Path) -> None:
    before = _load(tmp_path, BASE).workspaces[0].policy_version
    after = _load(tmp_path, BASE.replace("[oncall]", "[sre]")).workspaces[0].policy_version
    assert before != after


def test_policy_version_ignores_agent_changes(tmp_path: Path) -> None:
    before = _load(tmp_path, BASE).workspaces[0].policy_version
    after = _load(tmp_path, BASE.replace("agent-one", "agent-two")).workspaces[0].policy_version
    assert before == after


@pytest.mark.parametrize(
    "bad",
    [
        BASE.replace("decision: allow", "decision: maybe"),
        BASE.replace("tool: read_thing", "tool: ''"),
        BASE + "unknown_top_level: 1\n",
        "workspaces: [",
    ],
)
def test_invalid_bootstrap_files_are_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(BootstrapConfigError):
        _load(tmp_path, bad)

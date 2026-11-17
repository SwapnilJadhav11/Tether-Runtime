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


# --- W3.1 (ADR-0020): permission maps, one rule per tool, evaluator version -----------------

PERMISSIONS = """
      role_permissions:
        operator: [thing:read, thing:write]
      agent_permissions:
        agent-one: [thing:read]
"""


def test_permission_maps_are_part_of_the_policy_and_its_version(tmp_path: Path) -> None:
    with_maps = _load(tmp_path, BASE + PERMISSIONS).workspaces[0]
    assert with_maps.policy.role_permissions == {"operator": ("thing:read", "thing:write")}
    assert with_maps.policy.agent_permissions == {"agent-one": ("thing:read",)}
    assert with_maps.policy_version != _load(tmp_path, BASE).workspaces[0].policy_version
    revoked = BASE + PERMISSIONS.replace("[thing:read, thing:write]", "[thing:read]")
    assert _load(tmp_path, revoked).workspaces[0].policy_version != with_maps.policy_version


def test_the_policy_version_covers_the_evaluator_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0009 rule 4: changing the built-in evaluator changes every policy version."""
    from tether.policy import config

    before = _load(tmp_path, BASE).workspaces[0].policy_version
    monkeypatch.setattr(config, "EVALUATOR_VERSION", config.EVALUATOR_VERSION + "-next")
    assert _load(tmp_path, BASE).workspaces[0].policy_version != before


@pytest.mark.parametrize(
    ("label", "bad"),
    [
        ("two rules for one tool", BASE + "        - tool: read_thing\n          decision: deny\n"),
        (
            "require_approval without approver roles",
            BASE.replace("          approver_roles: [oncall]\n", ""),
        ),
        (
            "approver roles on an allow rule",
            BASE.replace(
                "          decision: allow\n",
                "          decision: allow\n          approver_roles: [oncall]\n",
            ),
        ),
        (
            "the removed requester_roles field",
            BASE.replace(
                "          decision: allow\n",
                "          decision: allow\n          requester_roles: [operator]\n",
            ),
        ),
        ("an unknown agent in agent_permissions", BASE + PERMISSIONS.replace("agent-one", "ghost")),
        ("a permission without an action", BASE + PERMISSIONS.replace("thing:read]", "thing]")),
        ("a wildcard permission", BASE + PERMISSIONS.replace("[thing:read]", "[thing:*]")),
        ("an uppercase permission", BASE + PERMISSIONS.replace("[thing:read]", "[Thing:Read]")),
    ],
)
def test_invalid_policies_are_rejected(tmp_path: Path, label: str, bad: str) -> None:
    with pytest.raises(BootstrapConfigError):
        _load(tmp_path, bad)

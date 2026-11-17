"""T-15 configuration semantics for per-tool approval expiry (W4.2, D-8, OD-7).

The per-tool value is an optional field of the tool's policy rule, so it is part of the policy
version (ADR-0020). The default is ``TETHER_APPROVAL_TTL_SECONDS``; the maximum is
``TETHER_APPROVAL_MAX_TTL_SECONDS``. A rule or a default above the maximum is refused at
``tether bootstrap apply`` (and at worker startup, see the integration tests), never clamped.
The values (default 3600 s, maximum 86400 s) are proposed and await owner confirmation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError
from typer.testing import CliRunner

from tether.cli import bootstrap as bootstrap_cli
from tether.cli.main import app
from tether.policy.config import (
    ApprovalExpiryError,
    BootstrapFile,
    PolicyRule,
    check_approval_expiry,
)
from tether.settings import ApprovalExpirySettings, Settings

runner = CliRunner()


def boot(expiry: int | None = None) -> BootstrapFile:
    rule: dict[str, Any] = {
        "tool": "write_thing",
        "decision": "require_approval",
        "approver_roles": ["oncall"],
    }
    if expiry is not None:
        rule["approval_expiry_seconds"] = expiry
    return BootstrapFile.model_validate(
        {
            "workspaces": [
                {
                    "id": "ws-one",
                    "agents": [{"id": "agent-one", "workload": "probe"}],
                    "policy": {"rules": [{"tool": "read_thing", "decision": "allow"}, rule]},
                }
            ]
        }
    )


def test_t15_the_defaults_are_the_proposed_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TETHER_APPROVAL_TTL_SECONDS", "TETHER_APPROVAL_MAX_TTL_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    limits = ApprovalExpirySettings()
    assert (limits.approval_ttl_seconds, limits.approval_max_ttl_seconds) == (3600, 86400)
    settings = Settings(database_url=SecretStr("postgresql://x"))
    assert (settings.approval_ttl_seconds, settings.approval_max_ttl_seconds) == (3600, 86400)


def test_t15_the_maximum_is_read_from_its_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TETHER_APPROVAL_MAX_TTL_SECONDS", "7200")
    monkeypatch.setenv("TETHER_APPROVAL_TTL_SECONDS", "600")
    limits = ApprovalExpirySettings()
    assert (limits.approval_ttl_seconds, limits.approval_max_ttl_seconds) == (600, 7200)


@pytest.mark.parametrize("value", [0, -1])
def test_t15_a_non_positive_expiry_is_refused(value: int) -> None:
    with pytest.raises(ValidationError):
        boot(value)


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_t15_an_expiry_is_only_allowed_on_a_require_approval_rule(decision: str) -> None:
    with pytest.raises(ValidationError, match="approval_expiry_seconds only on require_approval"):
        PolicyRule.model_validate(
            {"tool": "read_thing", "decision": decision, "approval_expiry_seconds": 60}
        )


def test_t15_the_tools_value_is_used_and_the_default_otherwise() -> None:
    policy = boot(120).workspaces[0].policy
    assert policy.approval_expiry_for("write_thing", 3600) == 120
    assert boot().workspaces[0].policy.approval_expiry_for("write_thing", 3600) == 3600
    assert policy.approval_expiry_for("read_thing", 3600) == 3600
    assert policy.approval_expiry_for("unknown_tool", 3600) == 3600


def test_t15_the_policy_version_changes_when_the_value_changes() -> None:
    """The value is a policy rule field, so it is hashed into the policy version (ADR-0020)."""
    versions = {boot(e).workspaces[0].policy_version for e in (None, 120, 121, 3600)}
    assert len(versions) == 4
    assert boot(120).workspaces[0].policy_version == boot(120).workspaces[0].policy_version


def test_t15_the_evaluator_version_was_bumped_for_the_new_rule_field() -> None:
    from tether.policy import config

    assert config.EVALUATOR_VERSION == "builtin-2"


@pytest.mark.parametrize("value", [1, 86399, 86400])
def test_t15_a_value_at_or_below_the_maximum_is_accepted(value: int) -> None:
    check_approval_expiry(boot(value), default_seconds=3600, max_seconds=86400)


def test_t15_a_value_above_the_maximum_is_refused_never_clamped() -> None:
    config = boot(86401)
    with pytest.raises(ApprovalExpiryError, match="write_thing sets approval_expiry_seconds 86401"):
        check_approval_expiry(config, default_seconds=3600, max_seconds=86400)
    rule = config.workspaces[0].policy.rule_for("write_thing")
    assert rule is not None
    assert rule.approval_expiry_seconds == 86401  # unchanged: refused, not clamped


def test_t15_a_default_above_the_maximum_is_refused() -> None:
    with pytest.raises(ApprovalExpiryError, match="default approval expiry"):
        check_approval_expiry(boot(), default_seconds=7201, max_seconds=7200)


def _write(tmp_path: Path, config: BootstrapFile) -> Path:
    import yaml

    path = tmp_path / "bootstrap.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("expiry", "env"),
    [(86401, {}), (None, {"TETHER_APPROVAL_TTL_SECONDS": "90000"})],
    ids=["rule-above-maximum", "default-above-maximum"],
)
def test_t15_bootstrap_apply_refuses_before_writing_anything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expiry: int | None,
    env: dict[str, str],
) -> None:
    monkeypatch.setenv("TETHER_MIGRATOR_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.delenv("TETHER_APPROVAL_TTL_SECONDS", raising=False)
    monkeypatch.delenv("TETHER_APPROVAL_MAX_TTL_SECONDS", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    applied: list[Any] = []
    monkeypatch.setattr(bootstrap_cli, "apply_bootstrap", lambda *args: applied.append(args))
    result = runner.invoke(app, ["bootstrap", "apply", str(_write(tmp_path, boot(expiry)))])
    assert result.exit_code == 1
    assert "approval expiry refused" in result.output
    assert applied == []


def test_t15_bootstrap_apply_accepts_a_value_within_the_maximum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TETHER_MIGRATOR_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.delenv("TETHER_APPROVAL_TTL_SECONDS", raising=False)
    monkeypatch.delenv("TETHER_APPROVAL_MAX_TTL_SECONDS", raising=False)
    applied: list[Any] = []
    monkeypatch.setattr(bootstrap_cli, "apply_bootstrap", lambda *args: applied.append(args))
    result = runner.invoke(app, ["bootstrap", "apply", str(_write(tmp_path, boot(86400)))])
    assert result.exit_code == 0, result.output
    assert len(applied) == 1

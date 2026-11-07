"""S1.5: WorkspaceScope and the scoped-repository contract (AD-04, invariant 13)."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from sqlalchemy import Column, MetaData, Table, Text

from tether.core.errors import ScopeViolationError
from tether.storage.repos.base import ScopedRepository
from tether.storage.scope import WorkspaceScope


@pytest.mark.parametrize("workspace_id", ["", 5, None, ["ws-a"]])
def test_scope_requires_a_non_empty_string(workspace_id: Any) -> None:
    with pytest.raises(ScopeViolationError):
        WorkspaceScope(workspace_id)


def test_scope_is_immutable() -> None:
    scope = WorkspaceScope("ws-a")
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.workspace_id = "ws-b"  # type: ignore[misc]


def test_job_scope_requires_the_job_and_run_workspaces_to_match() -> None:
    scope = WorkspaceScope.for_job(job_workspace_id="ws-a", run_workspace_id="ws-a")
    assert scope.workspace_id == "ws-a"
    with pytest.raises(ScopeViolationError):
        WorkspaceScope.for_job(job_workspace_id="ws-a", run_workspace_id="ws-b")


class Probe(ScopedRepository):
    table = Table("probe", MetaData(), Column("id", Text), Column("workspace_id", Text))


@pytest.mark.parametrize("scope", [None, "ws-a", object()])
def test_repository_requires_a_workspace_scope(scope: Any) -> None:
    with pytest.raises(TypeError):
        Probe(object(), scope)  # type: ignore[arg-type]


def test_repository_table_must_be_workspace_scoped() -> None:
    with pytest.raises(TypeError):

        class Unscoped(ScopedRepository):
            table = Table("unscoped", MetaData(), Column("id", Text))

"""WorkspaceScope: the only way to reach workspace data (AD-04, ADR-0013, invariant 13).

Request handlers build a scope from the authenticated principal (S1.8). Worker code builds it
from a job, after checking that the job and its run belong to the same workspace.
"""

from __future__ import annotations

from dataclasses import dataclass

from tether.core.errors import ScopeViolationError
from tether.core.ids import WorkspaceId


@dataclass(frozen=True, slots=True)
class WorkspaceScope:
    workspace_id: WorkspaceId

    def __post_init__(self) -> None:
        if type(self.workspace_id) is not str or not self.workspace_id:
            raise ScopeViolationError("a workspace scope needs a non-empty workspace ID")

    @classmethod
    def for_job(cls, *, job_workspace_id: str, run_workspace_id: str) -> WorkspaceScope:
        """Worker-side scope: the job and the run it targets must share a workspace."""
        if job_workspace_id != run_workspace_id:
            raise ScopeViolationError("the job and its run belong to different workspaces")
        return cls(WorkspaceId(job_workspace_id))

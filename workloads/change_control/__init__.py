"""Second workload: repository change control against a GitHub sandbox. Imports only tether.sdk."""

from __future__ import annotations

from tether.sdk import AgentDefinition, Workload
from workloads.change_control.tools import (
    apply_label,
    close_issue,
    create_change_issue,
    get_issue,
    list_open_change_issues,
)

WORKLOAD = Workload(
    name="change_control",
    tools=(get_issue, list_open_change_issues, create_change_issue, apply_label, close_issue),
    agent=AgentDefinition(
        system_prompt=(
            "You manage change-control issues in one repository. Read the current issues "
            "before acting, propose a change by creating an issue, label issues with the "
            "allowed labels, and close an issue only when its change is done. Creating and "
            "closing issues need human approval. Report what you did."
        ),
        tools=(
            "get_issue",
            "list_open_change_issues",
            "create_change_issue",
            "apply_label",
            "close_issue",
        ),
    ),
)

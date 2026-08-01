from typing import Any

from sentinel.agent.tools import ToolRegistry
from sentinel.audit import AuditLogger
from sentinel.config import Settings
from sentinel.integrations.github import GitHubClient
from sentinel.integrations.grafana import GrafanaClient
from sentinel.models import OperationRequest, Principal, Role, ToolResult
from sentinel.policy import PolicyEngine


class _ArgoStub:
    def get_status(self, _request: OperationRequest, _args: dict[str, Any]) -> ToolResult:
        return ToolResult(True, "unexpected")

    diff = get_status
    list_applications = get_status
    list_out_of_sync = get_status
    list_pods = get_status
    get_logs = get_status
    get_environment_variables = get_status


def test_policy_denial_is_marked_for_slack_rendering() -> None:
    settings = Settings(
        operational_targets={
            "commerce": {"application": "commerce", "environment": "production"}
        }
    )
    registry = ToolRegistry(
        PolicyEngine(), GitHubClient(settings), AuditLogger(), _ArgoStub(), GrafanaClient(settings)
    )
    request = OperationRequest(
        "request", "channel", "commerce status", Principal("dev", "dev", None, {Role.DEV})
    )

    result = registry.execute(
        request, "argocd_get_status", {"service": "commerce", "environment": "production"}
    )

    assert not result.ok
    assert result.data["error_kind"] == "denied"

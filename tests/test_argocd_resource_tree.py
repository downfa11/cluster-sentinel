from typing import Any

from sentinel.config import Settings
from sentinel.integrations.argocd import ArgoCdClient
from sentinel.models import OperationRequest, Principal, Role


def _request() -> OperationRequest:
    return OperationRequest(
        "request", "channel", "commerce logs", Principal("user", "user", None, {Role.ADMIN})
    )


class _ResourceTreeArgo(ArgoCdClient):
    def __init__(self) -> None:
        super().__init__(Settings())
        self.log_path = ""
        self.log_params: dict[str, str] = {}

    def _get_json(self, path: str) -> dict[str, Any]:
        assert path == "/api/v1/applications/commerce/resource-tree"
        return {
            "nodes": [
                {"kind": "Deployment", "name": "commerce-api", "namespace": "commerce"},
                {
                    "kind": "Pod",
                    "name": "commerce-api-abc",
                    "namespace": "commerce",
                    "health": {"status": "Healthy"},
                    "info": [{"name": "Status Reason", "value": "Running"}],
                },
            ]
        }

    def _get_text(self, path: str, params: dict[str, str]) -> str:
        self.log_path = path
        self.log_params = params
        return "ready\n"


def test_argocd_resource_tree_discovers_pods_and_uses_optional_container() -> None:
    argo = _ResourceTreeArgo()

    pods = argo.list_pods(_request(), {"_application": "commerce"})
    logs = argo.get_logs(_request(), {"_application": "commerce", "tail_lines": 100})

    assert "commerce/commerce-api-abc — Running" in pods.message
    assert argo.log_path.endswith("/commerce/pods/commerce-api-abc/logs")
    assert argo.log_params == {"namespace": "commerce", "tailLines": "100"}
    assert logs.data["slack_code_block"] == "```\nready\n```"

from sentinel.slack.notify import format_alert
from sentinel.agent.orchestrator import AgentOrchestrator
from sentinel.agent.tools import ToolRegistry
from sentinel.audit import AuditLogger
from sentinel.config import Settings
from sentinel.integrations.argocd import ArgoCdClient
from sentinel.integrations.github import GitHubClient, GitOpsPullRequestFactory
from sentinel.integrations.grafana import GrafanaClient
from sentinel.models import OperationRequest, Principal, Role, ToolResult
from sentinel.policy import PolicyEngine


def make_request(roles: set[Role]) -> OperationRequest:
    return OperationRequest(
        request_id="req-1",
        channel_id="C1",
        text="api를 staging에 ghcr.io/example/api:v1 버전으로 올려줘",
        principal=Principal(user_id="U1", slack_user_id="U1", github_username=None, roles=roles),
    )


def test_policy_denies_dev_production_deploy_tool() -> None:
    decision = PolicyEngine().authorize_tool_call(
        make_request({Role.DEV}),
        "github_create_deploy_pr",
        {"service": "api", "environment": "production"},
    )

    assert not decision.allowed
    assert "production" in decision.reason


def test_policy_allows_operator_non_prod_restart_tool() -> None:
    decision = PolicyEngine().authorize_tool_call(
        make_request({Role.OPERATOR}),
        "github_create_restart_pr",
        {"service": "api", "environment": "staging"},
    )

    assert decision.allowed


def test_policy_denies_dev_production_read_tool() -> None:
    decision = PolicyEngine().authorize_tool_call(
        make_request({Role.DEV}),
        "argocd_get_status",
        {"service": "api", "environment": "production"},
    )

    assert not decision.allowed


def test_deploy_pr_patches_values_yaml() -> None:
    draft = GitOpsPullRequestFactory(Settings()).deploy(
        make_request({Role.OPERATOR}),
        {"service": "api", "environment": "staging", "image_tag": "ghcr.io/example/api:v1"},
    )

    rendered = draft.mutations[0].render("image:\n  repository: old\n  tag: old\n")

    assert "repository: ghcr.io/example/api" in rendered
    assert "tag: v1" in rendered
    assert "lastRequestId: req-1" in rendered


def test_tool_registry_requires_deploy_arguments() -> None:
    settings = Settings()
    registry = ToolRegistry(
        PolicyEngine(),
        GitHubClient(settings),
        AuditLogger(),
        ArgoCdClient(settings),
        GrafanaClient(settings),
    )

    result = registry.execute(make_request({Role.OPERATOR}), "github_create_deploy_pr", {"service": "api"})

    assert not result.ok
    assert "missing required" in result.message


def test_tool_registry_schemas_include_required_fields() -> None:
    settings = Settings()
    registry = ToolRegistry(
        PolicyEngine(),
        GitHubClient(settings),
        AuditLogger(),
        ArgoCdClient(settings),
        GrafanaClient(settings),
    )
    deploy_schema = next(schema for schema in registry.schemas if schema["name"] == "github_create_deploy_pr")

    assert set(deploy_schema["parameters"]["required"]) == {"service", "environment", "image_tag"}


def test_access_pr_grant_patches_users_yaml() -> None:
    draft = GitOpsPullRequestFactory(Settings()).access_change(
        make_request({Role.ADMIN}),
        {"action": "grant", "user": "alice@example.com", "role": "operator", "group": "api"},
    )

    rendered = draft.mutations[0].render(
        "users:\n"
        "  - email: alice@example.com\n"
        "    status: active\n"
        "    roles:\n"
        "      - dev\n"
        "    groups: []\n"
    )

    assert draft.mutations[0].path == "access/users.yaml"
    assert "email: alice@example.com" in rendered
    assert "- dev" in rendered
    assert "- operator" in rendered
    assert "- api" in rendered
    assert "lastAccessAction: grant" in rendered


def test_access_pr_offboard_disables_user_and_clears_memberships() -> None:
    draft = GitOpsPullRequestFactory(Settings()).access_change(
        make_request({Role.ADMIN}),
        {"action": "offboard", "user": "alice@example.com"},
    )

    rendered = draft.mutations[0].render(
        "users:\n"
        "  - email: alice@example.com\n"
        "    status: active\n"
        "    roles:\n"
        "      - operator\n"
        "    groups:\n"
        "      - api\n"
    )

    assert "status: inactive" in rendered
    assert "roles: []" in rendered
    assert "groups: []" in rendered

def test_access_tool_name_determines_action() -> None:
    settings = Settings()
    registry = ToolRegistry(
        PolicyEngine(),
        GitHubClient(settings),
        AuditLogger(),
        ArgoCdClient(settings),
        GrafanaClient(settings),
    )
    request = make_request({Role.ADMIN})

    result = registry.execute(request, "github_create_revoke_pr", {"user": "alice@example.com", "role": "operator"})

    assert result.ok
    assert result.data["title"] == "sentinel: revoke alice@example.com"

def test_slack_alert_format_includes_severity_title_and_body() -> None:
    text = format_alert("warning", "Sentinel test", "body")

    assert text == "[WARNING] Sentinel test\nbody"

def test_access_grant_requires_role_or_group() -> None:
    settings = Settings()
    registry = ToolRegistry(
        PolicyEngine(),
        GitHubClient(settings),
        AuditLogger(),
        ArgoCdClient(settings),
        GrafanaClient(settings),
    )

    result = registry.execute(make_request({Role.ADMIN}), "github_create_grant_pr", {"user": "alice@example.com"})

    assert not result.ok
    assert "role or group" in result.message


class _FakeFunctionCall:
    type = "function_call"

    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeResponses:
    def create(self, **_kwargs: object) -> object:
        return type(
            "FakeResponse",
            (),
            {
                "output": [
                    _FakeFunctionCall("github_create_deploy_pr", "{}"),
                    _FakeFunctionCall("github_create_restart_pr", "{}"),
                ]
            },
        )()


class _FakeClient:
    responses = _FakeResponses()


class _FakeMcp:
    def list_tools(self) -> list[object]:
        return []

    def openai_tool_schemas(self) -> list[dict[str, object]]:
        return []

    def call_tool(self, _request: OperationRequest, _call: object) -> ToolResult:
        raise AssertionError("multiple write tools must be rejected before execution")


def test_orchestrator_rejects_multiple_write_tools() -> None:
    orchestrator = AgentOrchestrator(Settings(openai_api_key="test"), _FakeMcp())
    orchestrator.client = _FakeClient()

    result = orchestrator.handle(make_request({Role.ADMIN}))

    assert not result.ok
    assert "multiple write tools" in result.message


def test_slack_dms_are_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("SENTINEL_SLACK_ALLOW_DMS", raising=False)

    assert Settings().slack_allow_dms is False
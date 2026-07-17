from pathlib import Path

import pytest

from sentinel.access.sync import AccessDirectory
from sentinel.agent.orchestrator import AgentOrchestrator
from sentinel.agent.tools import ToolRegistry
from sentinel.audit import AuditLogger
from sentinel.config import Settings
from sentinel.identity import IdentityResolver
from sentinel.integrations.argocd import ArgoCdClient
from sentinel.integrations.github import GitHubClient, GitOpsPullRequestFactory
from sentinel.integrations.grafana import GrafanaClient
from sentinel.models import OperationRequest, Principal, Role, ToolResult
from sentinel.policy import PolicyEngine
from sentinel.slack.notify import format_alert


def make_request(roles: set[Role]) -> OperationRequest:
    return OperationRequest(
        request_id="req-1",
        channel_id="C1",
        text="commerce-api production deploy",
        principal=Principal(user_id="U1", slack_user_id="U1", github_username=None, roles=roles),
    )


def target_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "gitops_targets": {
            "commerce-api": {
                "path": "clusters/home/commerce/api.yaml",
                "repository": "ghcr.io/rclc2/commerce-api",
                "application": "commerce",
                "environment": "production",
            }
        },
        "argocd_app_name_template": "{service}",
    }
    values.update(overrides)
    return Settings(**values)


def test_policy_denies_dev_production_deploy_tool() -> None:
    decision = PolicyEngine().authorize_tool_call(
        make_request({Role.DEV}),
        "github_create_deploy_pr",
        {"service": "commerce-api", "environment": "production"},
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
        {"service": "commerce", "environment": "production"},
    )
    assert not decision.allowed


def test_deploy_pr_only_replaces_allowlisted_digest() -> None:
    digest = "sha256:" + "a" * 64
    factory = GitOpsPullRequestFactory(target_settings())
    draft = factory.deploy(
        make_request({Role.ADMIN}),
        {"service": "commerce-api", "environment": "production", "image_tag": digest},
    )
    current = (
        "apiVersion: apps/v1\nkind: Deployment\nspec:\n  template:\n    metadata:\n"
        "      labels: {app: commerce-api}\n    spec:\n      containers:\n"
        "        - name: api\n"
        "          image: ghcr.io/rclc2/commerce-api@sha256:" + "b" * 64 + "\n"
    )
    rendered = draft.mutations[0].render(current)
    assert f"image: ghcr.io/rclc2/commerce-api@{digest}" in rendered
    assert draft.mutations[0].path == "clusters/home/commerce/api.yaml"
    assert "## External Apply Steps" in draft.body
    assert draft.action == "deploy"


@pytest.mark.parametrize(
    "image",
    ["latest", "v1", "ghcr.io/other/api@sha256:" + "a" * 64],
)
def test_deploy_pr_rejects_mutable_or_wrong_repository(image: str) -> None:
    with pytest.raises(RuntimeError):
        GitOpsPullRequestFactory(target_settings()).deploy(
            make_request({Role.ADMIN}),
            {"service": "commerce-api", "environment": "production", "image_tag": image},
        )


def test_deploy_pr_rejects_unknown_service_and_environment() -> None:
    factory = GitOpsPullRequestFactory(target_settings())
    digest = "sha256:" + "a" * 64
    with pytest.raises(RuntimeError, match="unsupported GitOps service"):
        factory.deploy(
            make_request({Role.ADMIN}),
            {"service": "arbitrary", "environment": "production", "image_tag": digest},
        )
    with pytest.raises(RuntimeError, match="unsupported environment"):
        factory.deploy(
            make_request({Role.ADMIN}),
            {"service": "commerce-api", "environment": "staging", "image_tag": digest},
        )


def test_restart_pr_adds_pod_template_annotation() -> None:
    draft = GitOpsPullRequestFactory(target_settings()).restart(
        make_request({Role.ADMIN}),
        {"service": "commerce-api", "environment": "production"},
    )
    current = (
        "apiVersion: apps/v1\nkind: Deployment\nspec:\n  template:\n    metadata:\n"
        "      labels: {app: commerce-api}\n    spec: {}\n"
    )
    rendered = draft.mutations[0].render(current)
    assert 'sentinel.dev/restartedAt: "req-1"' in rendered
    assert (
        rendered.replace('      annotations:\n        sentinel.dev/restartedAt: "req-1"\n', "")
        == current
    )


def test_argocd_component_target_maps_to_application() -> None:
    client = ArgoCdClient(target_settings())
    assert (
        client._app_name(
            make_request({Role.ADMIN}),
            {"service": "commerce-api", "environment": "production"},
        )
        == "commerce"
    )


def test_access_pr_uses_current_single_role_schema() -> None:
    factory = GitOpsPullRequestFactory(target_settings())
    current = (
        "users:\n  - id: alice\n    name: Alice\n    email: alice@example.com\n"
        "    role: dev\n    status: active\n"
    )
    grant = factory.access_change(
        make_request({Role.ADMIN}),
        {"action": "grant", "user": "alice@example.com", "role": "operator"},
    )
    rendered = grant.mutations[0].render(current)
    assert "role: operator" in rendered
    assert "roles:" not in rendered
    assert "sentinel:" not in rendered

    offboard = factory.access_change(
        make_request({Role.ADMIN}),
        {"action": "offboard", "user": "alice@example.com"},
    )
    assert "status: inactive" in offboard.mutations[0].render(rendered)


def test_access_onboard_and_lookup_use_current_keys() -> None:
    factory = GitOpsPullRequestFactory(target_settings())
    draft = factory.access_change(
        make_request({Role.ADMIN}),
        {
            "action": "onboard",
            "user": "bob@example.com",
            "name": "Bob",
            "github_username": "bob-gh",
            "slack_user_id": "U2",
            "role": "gui-user",
        },
    )
    rendered = draft.mutations[0].render("users: []\n")
    assert "id: bob" in rendered
    assert "github: bob-gh" in rendered
    assert "slack: U2" in rendered
    assert factory.find_access_user(rendered, "U2") == {
        "id": "bob",
        "name": "Bob",
        "email": "bob@example.com",
        "role": "gui-user",
        "status": "active",
        "github": "bob-gh",
        "slack": "U2",
    }


def test_identity_and_access_sync_accept_current_schema(tmp_path: Path) -> None:
    access = tmp_path / "access"
    access.mkdir()
    users = (
        "users:\n  - id: alice\n    email: alice@example.com\n    github: alice-gh\n"
        "    slack: U2\n    role: operator\n    status: active\n"
    )
    (access / "users.yaml").write_text(users, encoding="utf-8")
    (access / "groups.yaml").write_text("groups: {}\n", encoding="utf-8")
    principal = IdentityResolver(
        Settings(access_users_path=str(access / "users.yaml"))
    ).resolve_slack_user("U2")
    assert principal.roles == {Role.OPERATOR}
    assert principal.github_username == "alice-gh"
    directory = AccessDirectory(access)
    assert directory.users[0].roles == {"operator"}
    assert directory.users[0].groups == {"operator"}


def test_tool_registry_requires_deploy_arguments() -> None:
    settings = target_settings()
    registry = ToolRegistry(
        PolicyEngine(),
        GitHubClient(settings),
        AuditLogger(),
        ArgoCdClient(settings),
        GrafanaClient(settings),
    )
    result = registry.execute(
        make_request({Role.OPERATOR}), "github_create_deploy_pr", {"service": "commerce-api"}
    )
    assert not result.ok
    assert "missing required" in result.message


def test_tool_registry_schemas_require_digest_and_role() -> None:
    settings = target_settings()
    registry = ToolRegistry(
        PolicyEngine(),
        GitHubClient(settings),
        AuditLogger(),
        ArgoCdClient(settings),
        GrafanaClient(settings),
    )
    deploy = next(
        schema for schema in registry.schemas if schema["name"] == "github_create_deploy_pr"
    )
    grant = next(
        schema for schema in registry.schemas if schema["name"] == "github_create_grant_pr"
    )
    assert set(deploy["parameters"]["required"]) == {"service", "environment", "image_tag"}
    assert grant["parameters"]["properties"]["role"]["enum"] == [
        "gui-user",
        "dev",
        "operator",
        "admin",
    ]
    assert "group" not in grant["parameters"]["properties"]


def test_access_grant_requires_role() -> None:
    settings = target_settings()
    registry = ToolRegistry(
        PolicyEngine(),
        GitHubClient(settings),
        AuditLogger(),
        ArgoCdClient(settings),
        GrafanaClient(settings),
    )
    result = registry.execute(
        make_request({Role.ADMIN}), "github_create_grant_pr", {"user": "alice@example.com"}
    )
    assert not result.ok
    assert "role" in result.message


def test_slack_alert_format_includes_severity_title_and_body() -> None:
    assert format_alert("warning", "Sentinel test", "body") == "[WARNING] Sentinel test\nbody"


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


class _FakeGeminiCompletions:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    def create(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        function = type(
            "Function", (), {"name": "argocd_get_status", "arguments": '{"service":"commerce"}'}
        )()
        call = type("ToolCall", (), {"function": function})()
        message = type("Message", (), {"tool_calls": [call]})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()


class _FakeGeminiClient:
    def __init__(self) -> None:
        self.completions = _FakeGeminiCompletions()
        self.chat = type("Chat", (), {"completions": self.completions})()


class _RecordingMcp:
    def __init__(self) -> None:
        self.call: object | None = None

    def list_tools(self) -> list[object]:
        return []

    def openai_tool_schemas(self) -> list[dict[str, object]]:
        return [
            {"name": "argocd_get_status", "description": "status", "parameters": {"type": "object"}}
        ]

    def call_tool(self, _request: OperationRequest, call: object) -> ToolResult:
        self.call = call
        return ToolResult(True, "ok")


def test_orchestrator_uses_gemini_chat_tool_calling() -> None:
    mcp = _RecordingMcp()
    orchestrator = AgentOrchestrator(Settings(gemini_api_key="test"), mcp)
    client = _FakeGeminiClient()
    orchestrator.client = client
    result = orchestrator.handle(make_request({Role.ADMIN}))
    assert result.ok
    assert orchestrator.provider == "gemini"
    assert client.completions.kwargs["model"] == "gemini-3.5-flash"
    tools = client.completions.kwargs["tools"]
    assert isinstance(tools, list)
    assert tools[0]["function"]["name"] == "argocd_get_status"
    assert getattr(mcp.call, "name") == "argocd_get_status"


def test_slack_dms_are_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SENTINEL_SLACK_ALLOW_DMS", raising=False)
    assert Settings().slack_allow_dms is False

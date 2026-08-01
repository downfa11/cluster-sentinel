from __future__ import annotations

import json
from typing import Any

from sentinel.agent.orchestrator import AgentOrchestrator
from sentinel.config import Settings
from sentinel.integrations.argocd import ArgoCdClient
from sentinel.models import McpToolCall, OperationRequest, Principal, Role, ToolResult
from sentinel.slack.app import SentinelSlackBot


def _request(
    text: str,
    conversation: tuple[tuple[str, str], ...] = (),
) -> OperationRequest:
    return OperationRequest(
        request_id="req-conversation",
        channel_id="C1",
        text=text,
        principal=Principal("U1", "U1", None, {Role.ADMIN}),
        conversation=conversation,
    )


class _RecordingMcp:
    def __init__(self) -> None:
        self.calls: list[McpToolCall] = []

    def list_tools(self) -> list[object]:
        return []

    def openai_tool_schemas(self) -> list[dict[str, object]]:
        return []

    def call_tool(self, _request: OperationRequest, call: McpToolCall) -> ToolResult:
        self.calls.append(call)
        return ToolResult(True, call.name)


def _operational_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "operational_targets": {
            "commerce": {
                "application": "commerce",
                "environment": "production",
            }
        }
    }
    values.update(overrides)
    return Settings(**values)


def test_common_reads_do_not_depend_on_llm_availability() -> None:
    mcp = _RecordingMcp()
    orchestrator = AgentOrchestrator(_operational_settings(), mcp)

    status = orchestrator.handle(_request("commerce 상태 알려줘"))
    logs = orchestrator.handle(_request("commerce 최근 로그 100줄 보여줘"))
    env = orchestrator.handle(_request("commerce에서 사용하는 환경변수명 목록 조회"))

    assert status.ok and logs.ok and env.ok
    assert [call.name for call in mcp.calls] == [
        "argocd_get_status",
        "argocd_get_logs",
        "argocd_get_environment_variables",
    ]
    assert mcp.calls[1].arguments["tail_lines"] == 100


class _ConversationResponses:
    def __init__(self) -> None:
        self.input: object | None = None

    def create(self, **kwargs: object) -> object:
        self.input = kwargs.get("input")
        return type(
            "Response",
            (),
            {
                "output": [],
                "output_text": "commerce를 기준으로 어떤 항목을 볼까요?",
            },
        )()


def test_model_can_answer_or_clarify_without_selecting_unrelated_tool() -> None:
    mcp = _RecordingMcp()
    orchestrator = AgentOrchestrator(
        _operational_settings(openai_api_key="test"),
        mcp,
    )
    responses = _ConversationResponses()
    orchestrator.client = type("Client", (), {"responses": responses})()
    request = _request(
        "그럼 다음에는?",
        (("user", "commerce 상태 알려줘"), ("assistant", "commerce는 Healthy입니다.")),
    )

    result = orchestrator.handle(request)

    assert result.ok
    assert result.data["response_kind"] == "conversation"
    assert result.message == "commerce를 기준으로 어떤 항목을 볼까요?"
    assert not mcp.calls
    assert "COMPLETE SLACK THREAD" in str(responses.input)
    assert "commerce는 Healthy입니다." in str(responses.input)


class _EnvironmentArgo(ArgoCdClient):
    def __init__(self) -> None:
        super().__init__(_operational_settings())

    def _get_json(self, path: str) -> dict[str, Any]:
        assert path.endswith("/applications/commerce/managed-resources")
        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "commerce-api", "namespace": "commerce"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "api",
                                "env": [
                                    {"name": "DATABASE_HOST", "value": "must-not-leak"},
                                    {
                                        "name": "API_TOKEN",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": "commerce-secret",
                                                "key": "api-token",
                                            }
                                        },
                                    },
                                ],
                                "envFrom": [
                                    {"configMapRef": {"name": "commerce-config"}},
                                    {"secretRef": {"name": "commerce-secret"}},
                                ],
                            }
                        ]
                    }
                }
            },
        }
        config_map = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "commerce-config", "namespace": "commerce"},
            "data": {"FEATURE_ENABLED": "true"},
        }
        return {
            "items": [
                {
                    "kind": "Deployment",
                    "name": "commerce-api",
                    "namespace": "commerce",
                    "targetState": json.dumps(deployment),
                },
                {
                    "kind": "ConfigMap",
                    "name": "commerce-config",
                    "namespace": "commerce",
                    "targetState": json.dumps(config_map),
                },
                {
                    "kind": "Secret",
                    "name": "commerce-secret",
                    "namespace": "commerce",
                    "liveState": json.dumps({"data": {"api-token": "must-not-leak"}}),
                },
            ]
        }


def test_environment_variable_tool_returns_names_without_values_or_secret_contents() -> None:
    result = _EnvironmentArgo().get_environment_variables(
        _request("env"),
        {"_application": "commerce"},
    )

    assert result.ok
    assert result.data["environment_variable_names"] == [
        "API_TOKEN",
        "DATABASE_HOST",
        "FEATURE_ENABLED",
    ]
    assert result.data["unresolved_secret_refs"] == ["commerce-secret"]
    rendered = json.dumps({"message": result.message, "data": result.data}, ensure_ascii=False)
    assert "must-not-leak" not in rendered
    assert "api-token" not in rendered


class _SlackRuntime:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def handle_text(self, **kwargs: Any) -> ToolResult:
        self.calls.append(kwargs)
        return ToolResult(True, "응답")


class _SlackClient:
    def reactions_add(self, **_kwargs: str) -> None:
        return None

    def reactions_remove(self, **_kwargs: str) -> None:
        return None


def test_slack_thread_passes_prior_turns_to_follow_up() -> None:
    bot = SentinelSlackBot.__new__(SentinelSlackBot)
    runtime = _SlackRuntime()
    bot.runtime = runtime  # type: ignore[assignment]
    replies: list[dict[str, Any]] = []

    bot._handle_event(
        {"ts": "100.1", "text": "commerce 상태", "user": "U1"},
        "C1",
        lambda **payload: replies.append(payload),
        _SlackClient(),
    )
    bot._handle_event(
        {"ts": "100.2", "thread_ts": "100.1", "text": "로그도", "user": "U1"},
        "C1",
        lambda **payload: replies.append(payload),
        _SlackClient(),
    )

    assert runtime.calls[0]["conversation"] == ()
    assert runtime.calls[1]["conversation"] == (
        ("user", "commerce 상태"),
        ("assistant", "응답"),
    )


def test_slack_thread_keeps_every_turn_without_an_eight_turn_limit() -> None:
    bot = SentinelSlackBot.__new__(SentinelSlackBot)
    key = ("C1", "thread-1")

    for index in range(20):
        bot._remember_turn(key, "user", f"질문-{index}")
        bot._remember_turn(key, "assistant", f"응답-{index}")

    conversation = bot._conversation(key)
    assert len(conversation) == 40
    assert conversation[0] == ("user", "질문-0")
    assert conversation[-1] == ("assistant", "응답-19")
    assert bot._conversation(("C1", "another-thread")) == ()

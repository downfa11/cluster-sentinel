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


def test_log_routing_uses_tokens_and_parses_complete_line_counts() -> None:
    mcp = _RecordingMcp()
    orchestrator = AgentOrchestrator(_operational_settings(), mcp)

    orchestrator.handle(_request("commerce catalog status"))
    orchestrator.handle(_request("commerce logs 300 lines"))
    orchestrator.handle(_request("commerce 로그 1000줄"))

    assert mcp.calls[0].name == "argocd_get_status"
    assert mcp.calls[1].name == "argocd_get_logs"
    assert mcp.calls[1].arguments["tail_lines"] == 300
    assert mcp.calls[2].name == "argocd_get_logs"
    assert mcp.calls[2].arguments["tail_lines"] == 500


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
    assert "\n- " not in result.message
    assert result.data["slack_code_block"].startswith(chr(96) * 3)
    assert "Deployment/commerce-api" in result.data["slack_code_block"]


class _SlackRuntime:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def handle_text(self, **kwargs: Any) -> ToolResult:
        self.calls.append(kwargs)
        return ToolResult(True, "응답")


class _SlackClient:
    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.messages = messages or []
        self.reply_calls: list[dict[str, Any]] = []

    def reactions_add(self, **_kwargs: str) -> None:
        return None

    def reactions_remove(self, **_kwargs: str) -> None:
        return None

    def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        self.reply_calls.append(kwargs)
        return {"messages": self.messages, "response_metadata": {"next_cursor": ""}}


def _slack_bot(runtime: _SlackRuntime | None = None) -> SentinelSlackBot:
    bot = SentinelSlackBot.__new__(SentinelSlackBot)
    if runtime is not None:
        bot.runtime = runtime  # type: ignore[assignment]
    return bot


def test_slack_thread_reloads_prior_turns_for_follow_up() -> None:
    runtime = _SlackRuntime()
    bot = _slack_bot(runtime)
    client = _SlackClient(
        [
            {"ts": "100.1", "text": "commerce 상태", "user": "U1"},
        ]
    )
    replies: list[dict[str, Any]] = []

    bot._handle_event(
        {"ts": "100.1", "text": "commerce 상태", "user": "U1"},
        "C1",
        lambda **payload: replies.append(payload),
        client,
    )
    client.messages = [
        {"ts": "100.1", "text": "commerce 상태", "user": "U1"},
        {"ts": "100.15", "bot_id": "B1", "blocks": replies[0]["blocks"]},
        {"ts": "100.2", "thread_ts": "100.1", "text": "로그도", "user": "U1"},
    ]
    bot._handle_event(
        {"ts": "100.2", "thread_ts": "100.1", "text": "로그도", "user": "U1"},
        "C1",
        lambda **payload: replies.append(payload),
        client,
    )

    assert runtime.calls[0]["conversation"] == ()
    assert runtime.calls[1]["conversation"] == (
        ("user", "commerce 상태"),
        ("assistant", "✅ Sentinel · 완료\n응답"),
    )


def test_slack_thread_context_survives_a_new_bot_instance() -> None:
    client = _SlackClient(
        [
            {"ts": "100.1", "text": "commerce 상태", "user": "U1"},
            {"ts": "100.2", "bot_id": "B1", "text": "commerce는 Healthy입니다."},
            {"ts": "100.3", "text": "로그도", "user": "U1"},
        ]
    )

    conversation = _slack_bot()._conversation(client, "C1", "100.1", "100.3")

    assert conversation == (
        ("user", "commerce 상태"),
        ("assistant", "commerce는 Healthy입니다."),
    )


def test_slack_thread_includes_rendered_tool_output() -> None:
    fence = chr(96) * 3
    client = _SlackClient(
        [
            {"ts": "100.1", "text": "최근 로그", "user": "U1"},
            {
                "ts": "100.2",
                "bot_id": "B1",
                "text": "로그 3줄",
                "blocks": [
                    {"type": "header", "text": {"type": "plain_text", "text": "완료"}},
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"{fence}\nline 1\nline 2\nline 3\n{fence}",
                        },
                    },
                ],
            },
            {"ts": "100.3", "text": "3번째 줄 설명해줘", "user": "U1"},
        ]
    )

    conversation = _slack_bot()._conversation(client, "C1", "100.1", "100.3")

    assert "line 3" in conversation[1][1]


def test_slack_thread_keeps_more_than_eight_turns_when_within_size_bound() -> None:
    messages = [
        {
            "ts": f"100.{index}",
            "text": f"질문-{index}" if index % 2 == 0 else f"응답-{index}",
            **({} if index % 2 == 0 else {"bot_id": "B1"}),
        }
        for index in range(40)
    ]

    conversation = _slack_bot()._conversation(
        _SlackClient(messages),
        "C1",
        "100.0",
        "not-present",
    )

    assert len(conversation) == 40
    assert conversation[0] == ("user", "질문-0")
    assert conversation[-1] == ("assistant", "응답-39")


def test_slack_thread_context_is_bounded_by_character_count() -> None:
    turns = [("user", "x" * 20_000) for _ in range(10)]

    conversation = SentinelSlackBot._bound_conversation(turns)

    assert conversation[0][1].startswith("[오래된 스레드 내용")
    assert sum(len(text) for _, text in conversation) <= (
        SentinelSlackBot.MAX_CONVERSATION_CHARS + len(conversation[0][1])
    )

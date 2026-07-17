from collections import deque
from typing import Any

from sentinel.config import Settings
from sentinel.models import ToolResult
from sentinel.slack.app import SentinelSlackBot


class _Runtime:
    def __init__(
        self,
        status: ToolResult | None = None,
        onboarding: ToolResult | None = None,
    ) -> None:
        self.status = status or ToolResult(
            True, "registration required", {"onboarding_status": "unregistered"}
        )
        self.onboarding = onboarding or ToolResult(
            True, "created", {"onboarding_status": "created"}
        )
        self.onboarding_calls: list[tuple[str, str, str]] = []

    def onboarding_status(self, _user: str, _channel: str) -> ToolResult:
        return self.status

    def handle_onboarding(self, user: str, channel: str, email: str) -> ToolResult:
        self.onboarding_calls.append((user, channel, email))
        return self.onboarding

    def format_result(self, result: ToolResult) -> str:
        return result.message


class _Client:
    def __init__(self) -> None:
        self.messages: list[dict[str, str]] = []
        self.reactions: list[tuple[str, str, str]] = []

    def chat_postMessage(self, **kwargs: str) -> dict[str, str]:
        self.messages.append(kwargs)
        return {"ts": f"reply-{len(self.messages)}"}

    def reactions_add(self, *, name: str, channel: str, timestamp: str) -> None:
        self.reactions.append((name, channel, timestamp))


def _bot(runtime: _Runtime) -> SentinelSlackBot:
    bot = SentinelSlackBot.__new__(SentinelSlackBot)
    bot.settings = Settings(
        slack_onboarding_channel_id="C-ONBOARD",
        slack_welcome_thread_ts="100.200",
    )
    bot.runtime = runtime  # type: ignore[assignment]
    bot._processed_join_events = set()
    bot._processed_join_order = deque(maxlen=1024)
    return bot


def test_new_member_gets_onboarding_instruction_in_fixed_thread() -> None:
    client = _Client()
    bot = _bot(_Runtime())

    bot._handle_member_join(
        {"channel": "C-ONBOARD", "user": "U-NEW", "event_ts": "1.2"},
        {"event_id": "Ev-1"},
        client,
        {"bot_user_id": "U-BOT"},
    )

    assert client.messages[0]["channel"] == "C-ONBOARD"
    assert client.messages[0]["thread_ts"] == "100.200"
    assert "/onboarding" in client.messages[0]["text"]
    assert "Tailscale" in client.messages[0]["text"]
    assert client.reactions == [("wave", "C-ONBOARD", "reply-1")]


def test_registered_member_is_welcomed_without_new_request() -> None:
    runtime = _Runtime(status=ToolResult(True, "done", {"onboarding_status": "already_registered"}))
    client = _Client()

    _bot(runtime)._handle_member_join(
        {"channel": "C-ONBOARD", "user": "U-KNOWN"},
        {"event_id": "Ev-2"},
        client,
        {},
    )

    assert "이미" in client.messages[0]["text"]
    assert client.reactions == [("white_check_mark", "C-ONBOARD", "reply-1")]


def test_duplicate_join_event_is_ignored() -> None:
    client = _Client()
    bot = _bot(_Runtime())
    event = {"channel": "C-ONBOARD", "user": "U-NEW"}
    body = {"event_id": "Ev-same"}

    bot._handle_member_join(event, body, client, {})
    bot._handle_member_join(event, body, client, {})

    assert len(client.messages) == 1


def test_bot_join_and_other_channel_are_ignored() -> None:
    client = _Client()
    bot = _bot(_Runtime())
    bot._handle_member_join(
        {"channel": "C-ONBOARD", "user": "U-BOT"}, {}, client, {"bot_user_id": "U-BOT"}
    )
    bot._handle_member_join({"channel": "C-OTHER", "user": "U-NEW"}, {}, client, {})
    assert client.messages == []


def test_onboarding_result_posts_only_status_to_welcome_thread() -> None:
    client = _Client()
    result = ToolResult(
        True,
        "draft created for se***@example.com",
        {"onboarding_status": "created", "pull_request_url": "https://example.test/pr/1"},
    )

    _bot(_Runtime())._post_onboarding_result(client, "U-NEW", result)

    assert "draft PR" in client.messages[0]["text"]
    assert "example.com" not in client.messages[0]["text"]
    assert client.reactions == [("memo", "C-ONBOARD", "reply-1")]


def test_slash_handler_acknowledges_and_calls_runtime() -> None:
    runtime = _Runtime()
    bot = _bot(runtime)
    handlers: dict[str, Any] = {}

    class _App:
        def event(self, _name: str) -> Any:
            return lambda function: function

        def command(self, name: str) -> Any:
            def register(function: Any) -> Any:
                handlers[name] = function
                return function

            return register

    bot.app = _App()  # type: ignore[assignment]
    bot._register_handlers()
    acknowledgements: list[bool] = []
    responses: list[dict[str, str]] = []
    client = _Client()

    handlers["/onboarding"](
        lambda: acknowledgements.append(True),
        {
            "channel_id": "C-ONBOARD",
            "user_id": "U-NEW",
            "text": "tailscale@example.com",
        },
        lambda **kwargs: responses.append(kwargs),
        client,
    )

    assert acknowledgements == [True]
    assert runtime.onboarding_calls == [("U-NEW", "C-ONBOARD", "tailscale@example.com")]
    assert responses == [{"response_type": "ephemeral", "text": "created"}]

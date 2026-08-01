from typing import Any

import pytest

from sentinel.models import ToolResult
from sentinel.slack.app import SentinelSlackBot


class _Runtime:
    def __init__(self, result: ToolResult | None = None, error: Exception | None = None) -> None:
        self.result = result or ToolResult(True, "done")
        self.error = error

    def handle_text(self, **_kwargs: str) -> ToolResult:
        if self.error:
            raise self.error
        return self.result

    def format_result(self, result: ToolResult) -> str:
        return result.message


class _Client:
    def __init__(self, fail_reactions: bool = False) -> None:
        self.fail_reactions = fail_reactions
        self.calls: list[tuple[str, str, str, str]] = []

    def conversations_replies(self, **_kwargs: Any) -> dict[str, Any]:
        return {"messages": [], "response_metadata": {"next_cursor": ""}}

    def reactions_add(self, *, name: str, channel: str, timestamp: str) -> None:
        if self.fail_reactions:
            raise RuntimeError("missing scope")
        self.calls.append(("add", name, channel, timestamp))

    def reactions_remove(self, *, name: str, channel: str, timestamp: str) -> None:
        if self.fail_reactions:
            raise RuntimeError("missing scope")
        self.calls.append(("remove", name, channel, timestamp))


def _bot(runtime: _Runtime) -> SentinelSlackBot:
    bot = SentinelSlackBot.__new__(SentinelSlackBot)
    bot.runtime = runtime  # type: ignore[assignment]
    return bot


@pytest.mark.parametrize(
    ("result", "final_reaction"),
    [(ToolResult(True, "done"), "white_check_mark"), (ToolResult(False, "denied"), "x")],
)
def test_event_reaction_moves_from_loading_to_final(
    result: ToolResult, final_reaction: str
) -> None:
    client = _Client()
    replies: list[dict[str, Any]] = []
    _bot(_Runtime(result=result))._handle_event(
        {"ts": "123.45", "text": "status", "user": "U1"},
        "C1",
        lambda **payload: replies.append(payload),
        client,
    )
    assert replies[0]["text"].endswith(result.message)
    assert replies[0]["thread_ts"] == "123.45"
    assert replies[0]["blocks"][0]["type"] == ("section" if result.ok else "header")
    assert client.calls == [
        ("add", "hourglass_flowing_sand", "C1", "123.45"),
        ("remove", "hourglass_flowing_sand", "C1", "123.45"),
        ("add", final_reaction, "C1", "123.45"),
    ]


def test_event_reply_stays_in_existing_thread() -> None:
    replies: list[dict[str, Any]] = []

    _bot(_Runtime())._handle_event(
        {
            "ts": "123.46",
            "thread_ts": "100.20",
            "text": "status",
            "user": "U1",
        },
        "C1",
        lambda **payload: replies.append(payload),
        _Client(),
    )

    assert replies[0]["thread_ts"] == "100.20"


def test_reaction_api_failure_does_not_block_reply() -> None:
    replies: list[dict[str, Any]] = []
    _bot(_Runtime())._handle_event(
        {"ts": "123.45", "text": "status", "user": "U1"},
        "C1",
        lambda **payload: replies.append(payload),
        _Client(fail_reactions=True),
    )
    assert replies[0]["text"] == "done"


def test_unexpected_failure_replaces_loading_with_x() -> None:
    client = _Client()
    with pytest.raises(RuntimeError, match="boom"):
        _bot(_Runtime(error=RuntimeError("boom")))._handle_event(
            {"ts": "123.45", "text": "status", "user": "U1"},
            "C1",
            lambda **_payload: None,
            client,
        )
    assert client.calls[-2:] == [
        ("remove", "hourglass_flowing_sand", "C1", "123.45"),
        ("add", "x", "C1", "123.45"),
    ]

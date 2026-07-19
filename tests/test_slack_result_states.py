from typing import Any

from sentinel.models import ToolResult
from sentinel.slack.messages import result_message


def _block_text(block: dict[str, Any]) -> str:
    text = block.get("text")
    return str(text.get("text", "")) if isinstance(text, dict) else ""


def test_result_message_distinguishes_failures_from_policy_denials() -> None:
    failed = result_message(ToolResult(False, "upstream service unavailable"))
    denied = result_message(
        ToolResult(False, "production read tools require operator role", {"error_kind": "denied"})
    )

    assert failed["blocks"][0]["text"]["text"] == "⚠️ Sentinel · 실패"
    assert denied["blocks"][0]["text"]["text"] == "⛔ Sentinel · 거부됨"


def test_result_message_splits_large_log_code_blocks() -> None:
    logs = "```\n" + "\n".join("x" * 80 for _ in range(80)) + "\n```"
    payload = result_message(ToolResult(True, "recent logs", {"slack_code_block": logs}))

    log_blocks = payload["blocks"][2:]
    assert len(log_blocks) > 1
    assert all(len(_block_text(block)) <= 2_900 for block in log_blocks)
    assert all(_block_text(block).startswith("```\n") for block in log_blocks)
    assert all(_block_text(block).endswith("\n```") for block in log_blocks)
    assert "```" not in payload["text"]

from typing import Any

from sentinel.agent.orchestrator import AgentOrchestrator
from sentinel.models import ToolResult
from sentinel.runtime import SentinelRuntime
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

    log_blocks = payload["blocks"][1:]
    assert len(log_blocks) > 1
    assert all(len(_block_text(block)) <= 2_900 for block in log_blocks)
    assert all(_block_text(block).startswith("```\n") for block in log_blocks)
    assert all(_block_text(block).endswith("\n```") for block in log_blocks)
    assert "```" not in payload["text"]


def test_result_message_escapes_untrusted_log_code_content() -> None:
    payload = result_message(
        ToolResult(True, "recent logs", {"slack_code_block": "```\n```\n<!channel>&\n```"})
    )

    rendered = _block_text(payload["blocks"][1])
    assert rendered.count("```") == 2
    assert "[code fence]" in rendered
    assert "&lt;!channel&gt;&amp;" in rendered


def test_multiple_tool_results_preserve_log_blocks_for_slack() -> None:
    summary = AgentOrchestrator._summarize_tool_results(
        [
            ToolResult(True, "recent logs", {"slack_code_block": "```\nready\n```"}),
            ToolResult(True, "no active alerts"),
        ]
    )

    payload = result_message(summary)
    assert summary.data["slack_code_blocks"] == ["```\nready\n```"]
    assert "ready" in _block_text(payload["blocks"][1])


def test_runtime_format_result_preserves_compound_log_blocks() -> None:
    summary = AgentOrchestrator._summarize_tool_results(
        [
            ToolResult(True, "healthy"),
            ToolResult(True, "recent logs", {"slack_code_block": "```\nready\n```"}),
        ]
    )

    rendered = SentinelRuntime.__new__(SentinelRuntime).format_result(summary)

    assert "healthy" in rendered
    assert "ready" in rendered


def test_result_message_caps_combined_log_blocks_to_slack_message_limit() -> None:
    large_log = "```\n" + "\n".join("x" * 80 for _ in range(160)) + "\n```"
    payload = result_message(
        ToolResult(True, "recent logs", {"slack_code_blocks": [large_log] * 20})
    )

    assert len(payload["blocks"]) == 50


def test_audit_metadata_redacts_log_blocks_recursively() -> None:
    metadata = SentinelRuntime._audit_metadata(
        {
            "slack_code_block": "```\nsecret log\n```",
            "results": [
                {"pod": "commerce-api", "slack_code_block": "```\nnested log\n```"},
                {"slack_code_blocks": ["```\nother log\n```"]},
            ],
        }
    )

    assert metadata == {"results": [{"pod": "commerce-api"}, {}]}

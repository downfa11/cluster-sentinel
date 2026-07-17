from typing import Any

from sentinel.models import ToolResult
from sentinel.slack.messages import alert_message, instruction_message, onboarding_message, result_message


def _block_text(block: dict[str, Any]) -> str:
    text = block.get("text")
    return str(text.get("text", "")) if isinstance(text, dict) else ""


def test_result_message_uses_one_block_kit_shape_and_disables_unfurls() -> None:
    payload = result_message(
        ToolResult(
            True,
            "draft pull request created",
            {"pull_request_url": "https://github.com/example/repo/pull/7"},
        )
    )

    assert payload["text"].startswith("✅ Sentinel · 완료")
    assert payload["blocks"][0] == {
        "type": "header",
        "text": {"type": "plain_text", "text": "✅ Sentinel · 완료", "emoji": True},
    }
    assert payload["blocks"][2]["type"] == "actions"
    assert payload["blocks"][2]["elements"][0]["url"].endswith("/pull/7")
    assert payload["mrkdwn"] is False
    assert payload["unfurl_links"] is False
    assert payload["unfurl_media"] is False


def test_alert_message_uses_standard_layout_and_escapes_untrusted_text() -> None:
    payload = alert_message("critical", "DB <failure>", "notify <!channel> & investigate")

    assert payload["blocks"][0]["text"]["text"] == "🚨 Sentinel · 긴급"
    assert payload["blocks"][1]["text"]["text"] == (
        "*DB &lt;failure&gt;*\nnotify &lt;!channel&gt; &amp; investigate"
    )
    assert payload["mrkdwn"] is False
    assert payload["unfurl_links"] is False
    assert payload["unfurl_media"] is False


def test_result_message_escapes_untrusted_mrkdwn() -> None:
    payload = result_message(ToolResult(False, "denied <!channel> & <script>"))
    section = _block_text(payload["blocks"][1])
    assert "<!channel>" not in section
    assert "&lt;!channel&gt;" in section
    assert "&amp;" in section


def test_database_table_is_split_into_valid_sized_code_blocks() -> None:
    lines = [f"| row-{index:03d} | {'x' * 80} |" for index in range(80)]
    table = "```\n" + "\n".join(lines) + "\n```"
    payload = result_message(
        ToolResult(
            True,
            "Read-only query completed for commerce",
            {
                "row_count": 80,
                "displayed_rows": 50,
                "truncated": True,
                "slack_table": table,
            },
        )
    )

    table_blocks = payload["blocks"][3:]
    assert len(table_blocks) > 1
    assert all(block["type"] == "section" for block in table_blocks)
    assert all(len(_block_text(block)) <= 2_900 for block in table_blocks)
    assert all(_block_text(block).startswith("```\n") for block in table_blocks)
    assert all(_block_text(block).endswith("\n```") for block in table_blocks)
    assert "```" not in payload["text"]
    assert "Rows: 80; displayed: 50 (truncated)" in payload["text"]


def test_onboarding_message_keeps_only_valid_slack_mentions() -> None:
    valid = onboarding_message("join_unregistered", "U012ABCDEF")
    invalid = onboarding_message("join_unregistered", "<!channel>")

    assert "<@U012ABCDEF>" in _block_text(valid["blocks"][1])
    assert "<!channel>" not in _block_text(invalid["blocks"][1])
    assert "Slack 사용자" in _block_text(invalid["blocks"][1])


def test_instruction_messages_share_the_standard_shape() -> None:
    for kind in ("wrong_channel", "usage"):
        payload = instruction_message(kind)
        assert payload["blocks"][0]["type"] == "header"
        assert payload["blocks"][1]["type"] == "section"
        assert payload["unfurl_links"] is False

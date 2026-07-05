from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any

from sentinel.config import Settings
from sentinel.env import load_dotenv


@dataclass(frozen=True)
class SlackPostResult:
    channel: str
    ts: str


class SlackNotifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def post_message(self, text: str, channel_id: str | None = None) -> SlackPostResult:
        target_channel = channel_id or self.settings.slack_alert_channel_id
        if not target_channel:
            raise RuntimeError("SENTINEL_SLACK_ALERT_CHANNEL_ID or --channel is required")
        if not self.settings.slack_bot_token:
            raise RuntimeError("SENTINEL_SLACK_BOT_TOKEN is required")

        try:
            from slack_sdk import WebClient
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise RuntimeError("slack_sdk is required to post Slack notifications") from exc

        response: Any = WebClient(token=self.settings.slack_bot_token).chat_postMessage(
            channel=target_channel,
            text=text,
        )
        return SlackPostResult(channel=str(response["channel"]), ts=str(response["ts"]))


def format_alert(severity: str, title: str, body: str | None) -> str:
    prefix = severity.upper()
    message = f"[{prefix}] {title}"
    if body:
        message += f"\n{body}"
    return message


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Post a Sentinel notification to Slack")
    parser.add_argument("--channel", help="Slack channel ID. Defaults to SENTINEL_SLACK_ALERT_CHANNEL_ID.")
    parser.add_argument("--severity", default="info", choices=["info", "warning", "critical"])
    parser.add_argument("--title", required=True)
    parser.add_argument("--body")
    args = parser.parse_args()

    text = format_alert(args.severity, args.title, args.body)
    result = SlackNotifier(Settings()).post_message(text, args.channel)
    print(f"posted channel={result.channel} ts={result.ts}")


if __name__ == "__main__":
    main()
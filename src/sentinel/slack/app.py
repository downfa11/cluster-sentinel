from __future__ import annotations

from typing import Any

from sentinel.config import Settings
from sentinel.runtime import SentinelRuntime


class SentinelSlackBot:
    def __init__(self, settings: Settings) -> None:
        try:
            from slack_bolt import App
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise RuntimeError("slack-bolt is required to run the Slack bot") from exc

        self.settings = settings
        self.runtime = SentinelRuntime(settings)
        self.app = App(token=settings.slack_bot_token or "xoxb-dry-run", signing_secret=settings.slack_signing_secret)
        self._register_handlers()

    def start(self) -> None:
        if not self.settings.slack_app_token:
            raise RuntimeError("SENTINEL_SLACK_APP_TOKEN is required for Socket Mode")
        from slack_bolt.adapter.socket_mode import SocketModeHandler

        SocketModeHandler(self.app, self.settings.slack_app_token).start()

    def _register_handlers(self) -> None:
        @self.app.event("app_mention")
        def mention_handler(event: dict[str, Any], say: Any) -> None:
            channel_id = str(event.get("channel", ""))
            if self.settings.slack_control_channels and channel_id not in self.settings.slack_control_channels:
                return
            result = self.runtime.handle_text(
                text=str(event.get("text", "")),
                slack_user_id=str(event.get("user", "")),
                channel_id=channel_id,
            )
            say(self.runtime.format_result(result))

        @self.app.event("message")
        def direct_message_handler(event: dict[str, Any], say: Any) -> None:
            if event.get("bot_id") or event.get("subtype"):
                return
            if event.get("channel_type") != "im":
                return
            if not self.settings.slack_allow_dms:
                return
            result = self.runtime.handle_text(
                text=str(event.get("text", "")),
                slack_user_id=str(event.get("user", "")),
                channel_id=str(event.get("channel", "")),
            )
            say(self.runtime.format_result(result))


def create_app(settings: Settings | None = None) -> Any:
    bot = SentinelSlackBot(settings or Settings())
    return bot.app

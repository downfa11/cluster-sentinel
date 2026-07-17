from __future__ import annotations

from typing import Any

from sentinel.config import Settings
from sentinel.runtime import SentinelRuntime


class SentinelSlackBot:
    LOADING_REACTION = "hourglass_flowing_sand"
    SUCCESS_REACTION = "white_check_mark"
    FAILURE_REACTION = "x"

    def __init__(self, settings: Settings) -> None:
        try:
            from slack_bolt import App
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise RuntimeError("slack-bolt is required to run the Slack bot") from exc

        self.settings = settings
        self.runtime = SentinelRuntime(settings)
        self.app = App(
            token=settings.slack_bot_token or "xoxb-dry-run",
            signing_secret=settings.slack_signing_secret,
        )
        self._register_handlers()

    def start(self) -> None:
        if not self.settings.slack_app_token:
            raise RuntimeError("SENTINEL_SLACK_APP_TOKEN is required for Socket Mode")
        from slack_bolt.adapter.socket_mode import SocketModeHandler

        SocketModeHandler(self.app, self.settings.slack_app_token).start()  # type: ignore[no-untyped-call]

    def _register_handlers(self) -> None:
        @self.app.event("app_mention")
        def mention_handler(event: dict[str, Any], say: Any, client: Any) -> None:
            channel_id = str(event.get("channel", ""))
            if (
                self.settings.slack_control_channels
                and channel_id not in self.settings.slack_control_channels
            ):
                return
            self._handle_event(event, channel_id, say, client)

        @self.app.event("message")
        def direct_message_handler(event: dict[str, Any], say: Any, client: Any) -> None:
            if event.get("bot_id") or event.get("subtype"):
                return
            if event.get("channel_type") != "im":
                return
            if not self.settings.slack_allow_dms:
                return
            self._handle_event(event, str(event.get("channel", "")), say, client)

    def _handle_event(
        self,
        event: dict[str, Any],
        channel_id: str,
        say: Any,
        client: Any,
    ) -> None:
        timestamp = str(event.get("ts", ""))
        self._reaction(client, "add", self.LOADING_REACTION, channel_id, timestamp)
        try:
            result = self.runtime.handle_text(
                text=str(event.get("text", "")),
                slack_user_id=str(event.get("user", "")),
                channel_id=channel_id,
            )
            say(self.runtime.format_result(result))
        except Exception:
            self._reaction(client, "remove", self.LOADING_REACTION, channel_id, timestamp)
            self._reaction(client, "add", self.FAILURE_REACTION, channel_id, timestamp)
            raise

        self._reaction(client, "remove", self.LOADING_REACTION, channel_id, timestamp)
        final_reaction = self.SUCCESS_REACTION if result.ok else self.FAILURE_REACTION
        self._reaction(client, "add", final_reaction, channel_id, timestamp)

    def _reaction(
        self,
        client: Any,
        action: str,
        name: str,
        channel_id: str,
        timestamp: str,
    ) -> None:
        if not channel_id or not timestamp:
            return
        try:
            method = getattr(client, f"reactions_{action}")
            method(name=name, channel=channel_id, timestamp=timestamp)
        except Exception:
            # A missing scope or transient Slack error must not suppress the actual answer.
            return


def create_app(settings: Settings | None = None) -> Any:
    bot = SentinelSlackBot(settings or Settings())
    return bot.app

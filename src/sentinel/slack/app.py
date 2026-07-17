from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

from sentinel.config import Settings
from sentinel.runtime import SentinelRuntime


class SentinelSlackBot:
    LOADING_REACTION = "hourglass_flowing_sand"
    SUCCESS_REACTION = "white_check_mark"
    FAILURE_REACTION = "x"
    ONBOARDING_REACTION = "wave"
    PENDING_REACTION = "hourglass_flowing_sand"
    CREATED_REACTION = "memo"

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
        self._processed_join_events: set[str] = set()
        self._processed_join_order: deque[str] = deque(maxlen=1024)
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

        @self.app.event("member_joined_channel")
        def member_joined_handler(
            event: dict[str, Any], body: dict[str, Any], client: Any, context: Any
        ) -> None:
            self._handle_member_join(event, body, client, context)

        @self.app.command("/onboarding")
        def onboarding_handler(
            ack: Callable[[], None],
            command: dict[str, Any],
            respond: Callable[..., None],
            client: Any,
        ) -> None:
            ack()
            channel_id = str(command.get("channel_id", ""))
            slack_user_id = str(command.get("user_id", ""))
            email = str(command.get("text", "")).strip()
            if channel_id != self.settings.slack_onboarding_channel_id:
                respond(
                    response_type="ephemeral",
                    text="이 명령은 지정된 Sentinel 온보딩 채널에서만 사용할 수 있습니다.",
                )
                return
            if len(email.split()) != 1 or "@" not in email:
                respond(
                    response_type="ephemeral",
                    text="사용법: `/onboarding tailscale-account@example.com`",
                )
                return
            result = self.runtime.handle_onboarding(slack_user_id, channel_id, email)
            respond(response_type="ephemeral", text=self.runtime.format_result(result))
            self._post_onboarding_result(client, slack_user_id, result)

    def _handle_member_join(
        self,
        event: dict[str, Any],
        body: dict[str, Any],
        client: Any,
        context: Any,
    ) -> None:
        channel_id = str(event.get("channel", ""))
        user_id = str(event.get("user", ""))
        if channel_id != self.settings.slack_onboarding_channel_id:
            return
        if not user_id or user_id == str((context or {}).get("bot_user_id", "")):
            return
        event_id = str(
            body.get("event_id") or f"{channel_id}:{user_id}:{event.get('event_ts', '')}"
        )
        if event_id in self._processed_join_events:
            return
        status = self.runtime.onboarding_status(user_id, channel_id)
        state = str(status.data.get("onboarding_status", "error"))
        if state == "already_registered":
            text = f"<@{user_id}>님, 이미 Sentinel 온보딩이 완료되어 있습니다. 환영합니다!"
            reaction = self.SUCCESS_REACTION
        elif state == "unregistered":
            text = (
                f"<@{user_id}>님, 환영합니다! 이 스레드에서 "
                "`/onboarding 본인의-Tailscale-이메일`을 실행해 등록을 요청해 주세요. "
                "Slack 이메일과 달라도 괜찮습니다."
            )
            reaction = self.ONBOARDING_REACTION
        else:
            text = (
                f"<@{user_id}>님, 환영합니다! 현재 등록 상태를 확인하지 못했습니다. "
                "잠시 후 `/onboarding 본인의-Tailscale-이메일`을 실행해 주세요."
            )
            reaction = self.FAILURE_REACTION
        timestamp = self._post_welcome_reply(client, text)
        if not timestamp:
            return
        self._reaction(client, "add", reaction, channel_id, timestamp)
        self._remember_join_event(event_id)

    def _post_onboarding_result(self, client: Any, user_id: str, result: Any) -> None:
        state = str(result.data.get("onboarding_status", "error"))
        messages = {
            "created": f"<@{user_id}>님의 온보딩 draft PR이 생성되었습니다. 검토와 병합을 기다립니다.",
            "pending": f"<@{user_id}>님의 온보딩 요청은 이미 검토 중입니다.",
            "already_registered": f"<@{user_id}>님은 이미 온보딩이 완료되어 있습니다.",
        }
        reactions = {
            "created": self.CREATED_REACTION,
            "pending": self.PENDING_REACTION,
            "already_registered": self.SUCCESS_REACTION,
        }
        timestamp = self._post_welcome_reply(
            client,
            messages.get(state, f"<@{user_id}>님의 온보딩 요청을 처리하지 못했습니다."),
        )
        self._reaction(
            client,
            "add",
            reactions.get(state, self.FAILURE_REACTION),
            str(self.settings.slack_onboarding_channel_id or ""),
            timestamp,
        )

    def _post_welcome_reply(self, client: Any, text: str) -> str:
        channel_id = self.settings.slack_onboarding_channel_id
        thread_ts = self.settings.slack_welcome_thread_ts
        if not channel_id or not thread_ts:
            return ""
        try:
            response = client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=text)
            return str(response.get("ts", ""))
        except Exception:
            return ""

    def _remember_join_event(self, event_id: str) -> None:
        if len(self._processed_join_order) == self._processed_join_order.maxlen:
            oldest = self._processed_join_order.popleft()
            self._processed_join_events.discard(oldest)
        self._processed_join_order.append(event_id)
        self._processed_join_events.add(event_id)

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

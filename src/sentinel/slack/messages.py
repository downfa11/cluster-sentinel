from __future__ import annotations

import re
from typing import Any, Literal

from sentinel.models import ToolResult

OnboardingMessage = Literal[
    "join_unregistered",
    "join_registered",
    "join_lookup_failed",
    "created",
    "pending",
    "already_registered",
    "failed",
]

_SLACK_USER_ID = re.compile(r"^[UW][A-Z0-9]+$")
_MAX_BLOCK_TEXT = 2_900


def result_message(result: ToolResult) -> dict[str, Any]:
    if result.ok:
        title = "✅ Sentinel · 완료"
    elif result.data.get("error_kind") == "denied":
        title = "⛔ Sentinel · 거부됨"
    else:
        title = "⚠️ Sentinel · 실패"
    body = _escape(result.message)
    fallback = f"{title}: {result.message}"
    blocks: list[dict[str, Any]] = [_header(title), _section(body)]

    pull_request_url = str(result.data.get("pull_request_url") or "")
    if pull_request_url:
        fallback += f"\nPR: {pull_request_url}"
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Draft PR 열기", "emoji": True},
                        "url": pull_request_url,
                        "action_id": "open_sentinel_pull_request",
                    }
                ],
            }
        )
    elif result.data.get("dry_run"):
        dry_run_title = str(result.data.get("title") or "change")
        fallback += f"\nDry run: {dry_run_title}"
        blocks.append(_context(f"Dry run · {_escape(dry_run_title)}"))

    table = str(result.data.get("slack_table") or "")
    if table:
        row_count = int(result.data.get("row_count") or 0)
        displayed = int(result.data.get("displayed_rows") or 0)
        truncated = bool(result.data.get("truncated"))
        fallback += f"\nRows: {row_count}; displayed: {displayed}" + (
            " (truncated)" if truncated else ""
        )
        blocks.append(
            {
                "type": "section",
                "fields": [
                    _field("전체 행", str(row_count)),
                    _field("표시 행", str(displayed)),
                    _field("결과 잘림", "예" if truncated else "아니요"),
                ],
            }
        )
        blocks.extend(_table_sections(table))

    code_block = str(result.data.get("slack_code_block") or "")
    if code_block:
        blocks.extend(_table_sections(code_block))

    return _payload(fallback, blocks)


def onboarding_message(kind: OnboardingMessage, user_id: str) -> dict[str, Any]:
    mention = _mention(user_id)
    content: dict[OnboardingMessage, tuple[str, str]] = {
        "join_unregistered": (
            "👋 Sentinel · 환영합니다",
            (
                f"{mention}님, Sentinel 채널에 오신 것을 환영합니다.\n"
                "`/onboarding 본인의-Tailscale-이메일`로 등록을 요청해 주세요. "
                "Slack 이메일과 달라도 괜찮습니다."
            ),
        ),
        "join_registered": (
            "✅ Sentinel · 온보딩 완료",
            f"{mention}님은 이미 등록되어 있습니다. 바로 읽기 전용 질문을 사용할 수 있습니다.",
        ),
        "join_lookup_failed": (
            "⚠️ Sentinel · 확인 필요",
            (
                f"{mention}님의 등록 상태를 확인하지 못했습니다. 잠시 후 "
                "`/onboarding 본인의-Tailscale-이메일`을 실행해 주세요."
            ),
        ),
        "created": (
            "📝 Sentinel · 온보딩 요청 생성",
            f"{mention}님의 draft PR이 생성되었습니다. 사람의 검토와 병합을 기다립니다.",
        ),
        "pending": (
            "⏳ Sentinel · 검토 대기 중",
            f"{mention}님의 온보딩 요청은 이미 검토 중이며 중복 PR은 만들지 않았습니다.",
        ),
        "already_registered": (
            "✅ Sentinel · 온보딩 완료",
            f"{mention}님은 이미 등록되어 있어 새 PR을 만들지 않았습니다.",
        ),
        "failed": (
            "❌ Sentinel · 온보딩 실패",
            f"{mention}님의 요청을 처리하지 못했습니다. 본인에게 표시된 오류를 확인해 주세요.",
        ),
    }
    title, body = content[kind]
    return _payload(f"{title}: {_plain(body)}", [_header(title), _section(body)])


def instruction_message(kind: Literal["wrong_channel", "usage"]) -> dict[str, Any]:
    if kind == "wrong_channel":
        title = "⛔ Sentinel · 사용할 수 없는 채널"
        body = "`/onboarding`은 지정된 Sentinel 온보딩 채널에서만 사용할 수 있습니다."
    else:
        title = "ℹ️ Sentinel · 사용법"
        body = "`/onboarding tailscale-account@example.com` 형식으로 입력해 주세요."
    return _payload(f"{title}: {_plain(body)}", [_header(title), _section(body)])


def alert_message(severity: str, title: str, body: str | None) -> dict[str, Any]:
    level = severity.lower()
    headings = {
        "info": "ℹ️ Sentinel · 알림",
        "warning": "⚠️ Sentinel · 경고",
        "critical": "🚨 Sentinel · 긴급",
    }
    heading = headings.get(level, headings["info"])
    content = f"*{_escape(title)}*"
    if body:
        content += f"\n{_escape(body)}"
    fallback = f"[{level.upper()}] {title}"
    if body:
        fallback += f"\n{body}"
    return _payload(fallback, [_header(heading), _section(content)])


def notification_message(text: str) -> dict[str, Any]:
    return _payload(
        f"Sentinel · 알림: {text}",
        [_header("ℹ️ Sentinel · 알림"), _section(_escape(text))],
    )


def _payload(text: str, blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "text": text,
        "blocks": blocks,
        "unfurl_links": False,
        "unfurl_media": False,
        "mrkdwn": False,
    }


def _header(text: str) -> dict[str, Any]:
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _field(label: str, value: str) -> dict[str, str]:
    return {"type": "mrkdwn", "text": f"*{label}*\n{_escape(value)}"}


def _table_sections(table: str) -> list[dict[str, Any]]:
    if not table.startswith("```") or not table.endswith("```"):
        return [_section(_escape(table))]
    content = table[3:-3].strip("\n")
    lines: list[str] = []
    for line in content.splitlines() or [""]:
        while len(line) > _MAX_BLOCK_TEXT - 8:
            lines.append(line[: _MAX_BLOCK_TEXT - 8])
            line = line[_MAX_BLOCK_TEXT - 8 :]
        lines.append(line)

    chunks: list[str] = []
    current: list[str] = []
    for line in lines:
        candidate = "\n".join([*current, line])
        if current and len(candidate) > _MAX_BLOCK_TEXT - 8:
            chunks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("\n".join(current))
    return [_section(f"```\n{chunk}\n```") for chunk in chunks]


def _mention(user_id: str) -> str:
    if _SLACK_USER_ID.fullmatch(user_id):
        return f"<@{user_id}>"
    return "Slack 사용자"


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plain(value: str) -> str:
    return re.sub(r"[`*]", "", re.sub(r"<@([A-Z0-9]+)>", r"@\1", value))

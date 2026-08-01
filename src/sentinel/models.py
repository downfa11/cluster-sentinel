from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


class Role(StrEnum):
    GUI_USER = "gui-user"
    DEV = "dev"
    OPERATOR = "operator"
    ADMIN = "admin"
    BOT = "bot"


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Principal:
    user_id: str
    slack_user_id: str
    github_username: str | None
    roles: set[Role]
    groups: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class OperationRequest:
    request_id: str
    channel_id: str
    text: str
    principal: Principal
    command: str = "natural_language"
    environment: str | None = None
    service: str | None = None
    conversation: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    required_approvals: list[str]
    constraints: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class McpToolCall:
    name: str
    arguments: dict[str, Any]


ToolSafety = Literal["read", "pr_write", "audit_write"]

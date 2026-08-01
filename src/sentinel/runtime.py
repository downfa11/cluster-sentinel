from __future__ import annotations

import uuid
from typing import Any

from sentinel.agent.mcp import SentinelMcpGateway
from sentinel.agent.orchestrator import AgentOrchestrator
from sentinel.agent.tools import ToolRegistry
from sentinel.audit import AuditLogger
from sentinel.config import Settings
from sentinel.database import DatabaseService
from sentinel.identity import IdentityResolver
from sentinel.integrations.argocd import ArgoCdClient
from sentinel.integrations.github import GitHubClient, GitOpsPullRequestFactory
from sentinel.integrations.grafana import GrafanaClient
from sentinel.models import OperationRequest, Principal, ToolResult
from sentinel.policy import PolicyEngine


class SentinelRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.identity = IdentityResolver(settings)
        readonly_channels = set(settings.slack_control_channels)
        if settings.slack_onboarding_channel_id:
            readonly_channels.add(settings.slack_onboarding_channel_id)
        self.policy = PolicyEngine(readonly_channels)
        self.audit = AuditLogger()
        self.github = GitHubClient(settings)
        self.access_factory = GitOpsPullRequestFactory(settings)
        self.tools = ToolRegistry(
            policy=self.policy,
            github=self.github,
            audit=self.audit,
            argocd=ArgoCdClient(settings),
            grafana=GrafanaClient(settings),
            database=DatabaseService(settings, self.audit) if settings.db_read_enabled else None,
        )
        self.mcp = SentinelMcpGateway(self.tools)
        self.agent = AgentOrchestrator(settings, self.mcp)

    def handle_text(
        self,
        text: str,
        slack_user_id: str,
        channel_id: str,
        conversation: tuple[tuple[str, str], ...] = (),
    ) -> ToolResult:
        principal = self.identity.resolve_slack_user(slack_user_id)
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            channel_id=channel_id,
            text=self._clean_slack_text(text),
            principal=principal,
            conversation=conversation,
        )

        self.audit.write("request.received", request, "success")
        decision = self.policy.authorize_request(request)
        if not decision.allowed:
            self.audit.write("request.denied", request, "denied", {"reason": decision.reason})
            return ToolResult(False, decision.reason, {"error_kind": "denied"})

        result = self.agent.handle(request)
        completion_metadata = self._audit_metadata(result.data)
        if "database" in result.data or "slack_table" in result.data:
            completion_metadata = {
                key: result.data[key]
                for key in ("database", "row_count", "displayed_rows", "truncated")
                if key in result.data
            }
        self.audit.write(
            "request.completed",
            request,
            "success" if result.ok else "error",
            completion_metadata,
        )
        return result

    @staticmethod
    def _audit_metadata(data: dict[str, Any]) -> dict[str, Any]:
        def redact(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    key: redact(item)
                    for key, item in value.items()
                    if key not in {"slack_code_block", "slack_code_blocks"}
                }
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value

        redacted = redact(data)
        return redacted if isinstance(redacted, dict) else {}

    def onboarding_status(self, slack_user_id: str, channel_id: str) -> ToolResult:
        request = self._onboarding_request(slack_user_id, channel_id)
        if channel_id != self.settings.slack_onboarding_channel_id:
            return ToolResult(False, "onboarding is only allowed in the configured channel")
        try:
            current = self.github.read_file("access/users.yaml")
            user = self.access_factory.find_access_user(current, slack_user_id)
        except Exception as exc:
            self.audit.write("onboarding.lookup", request, "error")
            return ToolResult(False, f"onboarding lookup failed ({exc.__class__.__name__})")
        if user and user.get("status", "active") == "active":
            return ToolResult(
                True,
                "Sentinel registration is already complete.",
                {"onboarding_status": "already_registered"},
            )
        return ToolResult(
            True,
            "Sentinel registration is required.",
            {"onboarding_status": "unregistered"},
        )

    def handle_onboarding(
        self, slack_user_id: str, channel_id: str, tailscale_email: str
    ) -> ToolResult:
        request = self._onboarding_request(slack_user_id, channel_id)
        email = tailscale_email.strip().lower()
        if channel_id != self.settings.slack_onboarding_channel_id:
            return ToolResult(False, "onboarding is only allowed in the configured channel")
        try:
            current = self.github.read_file("access/users.yaml")
            slack_user = self.access_factory.find_access_user(current, slack_user_id)
            email_user = self.access_factory.find_access_user(current, email)
            if slack_user:
                registered_email = str(slack_user.get("email") or "").lower()
                if registered_email and registered_email != email:
                    return ToolResult(
                        False,
                        "this Slack user is already linked to a different Tailscale account",
                    )
                if slack_user.get("status", "active") == "active":
                    return ToolResult(
                        True,
                        f"Sentinel registration already exists for {self._mask_email(email)}.",
                        {"onboarding_status": "already_registered"},
                    )
            if email_user:
                registered_slack = str(
                    email_user.get("slack_user_id") or email_user.get("slack") or ""
                )
                if registered_slack and registered_slack != slack_user_id:
                    return ToolResult(
                        False,
                        "this Tailscale account is already linked to another Slack user",
                    )

            args = {
                "action": "onboard",
                "user": email,
                "email": email,
                "slack_user_id": slack_user_id,
                "role": str((slack_user or email_user or {}).get("role") or "gui-user"),
            }
            result = self.github.create_pr(
                request, self.access_factory.access_change(request, args)
            )
        except Exception as exc:
            self.audit.write("onboarding.completed", request, "error")
            return ToolResult(False, f"onboarding failed ({exc.__class__.__name__})")

        status = "pending" if result.data.get("already_pending") else "created"
        result.data["onboarding_status"] = status
        result.data["tailscale_email"] = self._mask_email(email)
        self.audit.write(
            "onboarding.completed",
            request,
            "success",
            {"onboarding_status": status},
        )
        return result

    def _onboarding_request(self, slack_user_id: str, channel_id: str) -> OperationRequest:
        return OperationRequest(
            request_id=str(uuid.uuid4()),
            channel_id=channel_id,
            text="slash onboarding",
            principal=Principal(slack_user_id, slack_user_id, None, set()),
            command="onboarding",
        )

    def _mask_email(self, email: str) -> str:
        local, separator, domain = email.partition("@")
        if not separator:
            return "invalid-account"
        return f"{local[:2]}***@{domain}"

    def format_result(self, result: ToolResult) -> str:
        status = (
            "OK"
            if result.ok
            else "DENIED"
            if result.data.get("error_kind") == "denied"
            else "FAILED"
        )
        details = ""
        if result.data.get("pull_request_url"):
            details = f"\nPR: {result.data['pull_request_url']}"
        elif result.data.get("dry_run"):
            details = f"\nDry run: {result.data.get('title')}"
        elif result.data.get("slack_table"):
            row_count = result.data.get("row_count", 0)
            displayed = result.data.get("displayed_rows", 0)
            truncated = bool(result.data.get("truncated"))
            suffix = " (truncated)" if truncated else ""
            details = (
                f"\nRows: {row_count}; displayed: {displayed}{suffix}\n{result.data['slack_table']}"
            )
        elif result.data.get("slack_code_block"):
            details = f"\n{result.data['slack_code_block']}"
        return f"Sentinel {status}: {result.message}{details}"

    def _clean_slack_text(self, text: str) -> str:
        tokens = text.strip().split()
        cleaned = [
            token for token in tokens if not (token.startswith("<@") and token.endswith(">"))
        ]
        return " ".join(cleaned)

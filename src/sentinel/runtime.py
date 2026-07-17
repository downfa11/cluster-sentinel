from __future__ import annotations

import uuid

from sentinel.agent.mcp import SentinelMcpGateway
from sentinel.agent.orchestrator import AgentOrchestrator
from sentinel.agent.tools import ToolRegistry
from sentinel.audit import AuditLogger
from sentinel.config import Settings
from sentinel.database import DatabaseService
from sentinel.identity import IdentityResolver
from sentinel.integrations.argocd import ArgoCdClient
from sentinel.integrations.github import GitHubClient
from sentinel.integrations.grafana import GrafanaClient
from sentinel.models import OperationRequest, ToolResult
from sentinel.policy import PolicyEngine


class SentinelRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.identity = IdentityResolver(settings)
        self.policy = PolicyEngine()
        self.audit = AuditLogger()
        github = GitHubClient(settings)
        self.tools = ToolRegistry(
            policy=self.policy,
            github=github,
            audit=self.audit,
            argocd=ArgoCdClient(settings),
            grafana=GrafanaClient(settings),
            database=DatabaseService(settings, self.audit) if settings.db_read_enabled else None,
        )
        self.mcp = SentinelMcpGateway(self.tools)
        self.agent = AgentOrchestrator(settings, self.mcp)

    def handle_text(self, text: str, slack_user_id: str, channel_id: str) -> ToolResult:
        principal = self.identity.resolve_slack_user(slack_user_id)
        request = OperationRequest(
            request_id=str(uuid.uuid4()),
            channel_id=channel_id,
            text=self._clean_slack_text(text),
            principal=principal,
        )

        self.audit.write("request.received", request, "success")
        decision = self.policy.authorize_request(request)
        if not decision.allowed:
            self.audit.write("request.denied", request, "denied", {"reason": decision.reason})
            return ToolResult(False, decision.reason)

        result = self.agent.handle(request)
        completion_metadata = result.data
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

    def format_result(self, result: ToolResult) -> str:
        status = "OK" if result.ok else "DENIED"
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
        return f"Sentinel {status}: {result.message}{details}"

    def _clean_slack_text(self, text: str) -> str:
        tokens = text.strip().split()
        cleaned = [
            token for token in tokens if not (token.startswith("<@") and token.endswith(">"))
        ]
        return " ".join(cleaned)

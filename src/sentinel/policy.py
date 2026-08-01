from __future__ import annotations

from sentinel.models import OperationRequest, PolicyDecision, Risk, Role


class PolicyEngine:
    """Fail-closed authorization boundary for LLM-selected MCP tool calls."""

    READ_TOOLS = {
        "argocd_get_status",
        "argocd_diff",
        "argocd_list_applications",
        "argocd_list_out_of_sync",
        "argocd_list_pods",
        "argocd_get_logs",
        "argocd_get_environment_variables",
        "grafana_alerts",
        "access_get_user",
    }
    DATABASE_READ_TOOLS = {"db_get_schema", "db_query_readonly"}
    DEPLOYMENT_PR_TOOLS = {
        "github_create_deploy_pr",
        "github_create_restart_pr",
        "github_create_rollback_pr",
    }
    ACCESS_PR_TOOLS = {
        "github_create_onboard_pr",
        "github_create_offboard_pr",
        "github_create_grant_pr",
        "github_create_revoke_pr",
    }
    PR_CREATOR_ROLES = {Role.GUI_USER, Role.DEV, Role.OPERATOR, Role.ADMIN}

    def __init__(self, readonly_channel_ids: set[str] | None = None) -> None:
        self.readonly_channel_ids = readonly_channel_ids or set()

    def _is_readonly_channel(self, request: OperationRequest) -> bool:
        return bool(
            request.principal.slack_user_id and request.channel_id in self.readonly_channel_ids
        )

    def authorize_request(self, request: OperationRequest) -> PolicyDecision:
        if not request.principal.slack_user_id:
            return PolicyDecision(False, "unknown Slack actor", [], {})
        if not request.principal.roles and not self._is_readonly_channel(request):
            return PolicyDecision(False, "Slack actor is not registered for Sentinel", [], {})
        return PolicyDecision(
            True, "authenticated Slack actor or approved read-only channel", [], {}
        )

    def authorize_command(self, request: OperationRequest) -> PolicyDecision:
        return self.authorize_request(request)

    def authorize_tool_call(
        self,
        request: OperationRequest,
        tool_name: str,
        args: dict[str, object],
    ) -> PolicyDecision:
        roles = request.principal.roles
        environment = str(args.get("environment") or request.environment or "")

        if tool_name == "audit_write":
            return PolicyDecision(False, "audit tool is internal only", [], {})

        if tool_name in self.DATABASE_READ_TOOLS:
            if self._is_readonly_channel(request):
                return PolicyDecision(
                    True,
                    "approved channel database read-only query",
                    [],
                    self._constraints_for(tool_name, args),
                )
            if Role.OPERATOR not in roles and Role.ADMIN not in roles:
                return PolicyDecision(
                    False, "production database queries require operator or admin role", [], {}
                )
            return PolicyDecision(
                True,
                "production database read-only query",
                [],
                self._constraints_for(tool_name, args),
            )

        if tool_name in self.READ_TOOLS:
            return self._authorize_read_tool(request, tool_name, args, environment)

        if tool_name in self.ACCESS_PR_TOOLS:
            if not roles.intersection(self.PR_CREATOR_ROLES):
                return PolicyDecision(
                    False, "access PR tools require a registered Sentinel user", [], {}
                )
            return PolicyDecision(
                True,
                "registered user access proposal PR",
                ["admin", "access-owner"],
                self._constraints_for(tool_name, args),
            )

        if tool_name in self.DEPLOYMENT_PR_TOOLS:
            if not roles.intersection(self.PR_CREATOR_ROLES):
                return PolicyDecision(
                    False, "deployment PR tools require a registered Sentinel user", [], {}
                )
            return PolicyDecision(
                True,
                "registered user deployment proposal PR",
                ["admin"],
                self._constraints_for(tool_name, args),
            )

        return PolicyDecision(False, f"unknown MCP tool: {tool_name}", [], {})

    def _authorize_read_tool(
        self,
        request: OperationRequest,
        tool_name: str,
        args: dict[str, object],
        environment: str,
    ) -> PolicyDecision:
        roles = request.principal.roles
        if tool_name == "access_get_user":
            target = str(args.get("user") or "")
            if target and target not in {
                request.principal.slack_user_id,
                request.principal.user_id,
            }:
                if Role.OPERATOR not in roles and Role.ADMIN not in roles:
                    return PolicyDecision(
                        False, "access lookup for other users requires operator role", [], {}
                    )
            return PolicyDecision(
                True, "self access lookup", [], self._constraints_for(tool_name, args)
            )

        if self._is_readonly_channel(request):
            return PolicyDecision(
                True,
                "approved channel read-only MCP tool",
                [],
                self._constraints_for(tool_name, args),
            )

        if environment == "production" and Role.OPERATOR not in roles and Role.ADMIN not in roles:
            return PolicyDecision(False, "production read tools require operator role", [], {})
        return PolicyDecision(
            True, "read-only MCP tool", [], self._constraints_for(tool_name, args)
        )

    def risk_for_tool(self, tool_name: str, args: dict[str, object]) -> Risk:
        if tool_name in self.ACCESS_PR_TOOLS:
            return Risk.CRITICAL
        if str(args.get("environment") or "") == "production":
            return Risk.HIGH
        if tool_name in self.DEPLOYMENT_PR_TOOLS:
            return Risk.MEDIUM
        return Risk.LOW

    def _constraints_for(self, tool_name: str, args: dict[str, object]) -> dict[str, object]:
        return {
            "tool": tool_name,
            "service": args.get("service"),
            "environment": args.get("environment"),
            "risk": self.risk_for_tool(tool_name, args).value,
        }

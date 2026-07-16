from __future__ import annotations

from typing import Any

from sentinel.agent.mcp import SentinelMcpGateway
from sentinel.agent.tools import parse_tool_arguments
from sentinel.config import Settings
from sentinel.models import McpToolCall, OperationRequest, ToolResult


class AgentOrchestrator:
    WRITE_TOOLS = {
        "github_create_deploy_pr",
        "github_create_restart_pr",
        "github_create_rollback_pr",
        "github_create_onboard_pr",
        "github_create_offboard_pr",
        "github_create_grant_pr",
        "github_create_revoke_pr",
    }

    def __init__(self, settings: Settings, mcp: SentinelMcpGateway) -> None:
        self.settings = settings
        self.mcp = mcp
        self.client, self.model = self._create_client(settings)

    def _create_client(self, settings: Settings) -> tuple[Any, str]:
        if settings.llm_provider == "gemini":
            api_key, model = settings.gemini_api_key, settings.gemini_model
            base_url = settings.gemini_base_url
        elif settings.llm_provider == "openai":
            api_key, model, base_url = settings.openai_api_key, settings.openai_model, None
        else:
            raise RuntimeError(f"Unsupported SENTINEL_LLM_PROVIDER: {settings.llm_provider}")
        if not api_key:
            return None, model
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise RuntimeError("openai is required when SENTINEL_OPENAI_API_KEY is set") from exc
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        return OpenAI(**kwargs), model

    def handle(self, request: OperationRequest) -> ToolResult:
        if not self.client:
            return ToolResult(
                False,
                f"LLM API key is required for provider {self.settings.llm_provider}.",
            )

        try:
            response = self.client.responses.create(
                model=self.model,
                instructions=self._instructions(),
                input=self._input(request),
                tools=self.mcp.openai_tool_schemas(),
            )
        except Exception as exc:
            return ToolResult(False, self._safe_openai_error(exc, request.request_id))

        tool_calls = [
            item
            for item in getattr(response, "output", [])
            if getattr(item, "type", None) == "function_call"
        ]
        write_calls = [item for item in tool_calls if str(getattr(item, "name", "")) in self.WRITE_TOOLS]
        if len(write_calls) > 1:
            names = ", ".join(str(getattr(item, "name", "")) for item in write_calls)
            return ToolResult(False, f"LLM selected multiple write tools; refusing to execute: {names}")

        tool_results: list[ToolResult] = []
        for item in tool_calls:
            args = parse_tool_arguments(getattr(item, "arguments", None))
            result = self.mcp.call_tool(request, McpToolCall(str(item.name), args))
            tool_results.append(result)
            if not result.ok:
                return result

        if tool_results:
            return self._summarize_tool_results(tool_results)
        return ToolResult(False, "LLM did not select an MCP tool")

    def _instructions(self) -> str:
        tools = "\n".join(f"- {tool.name}: {tool.description}" for tool in self.mcp.list_tools())
        return (
            "You are Sentinel, an AI GitOps DevOps Agent inside Slack. "
            "Users speak naturally in Korean or English. Infer the operational intent, service, environment, "
            "version, user, role, and reason from the message. "
            "Use MCP tools to perform approved work. "
            "You must never execute shell commands, kubectl, terraform, ssh, direct Kubernetes mutation, or secret reads. "
            "For any write operation, call exactly one GitHub PR creation MCP tool. "
            "If required information is missing, do not guess dangerous values; return no tool. "
            "Available MCP tools:\n"
            f"{tools}"
        )

    def _input(self, request: OperationRequest) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            f"Slack message: {request.text}\n"
                            f"Actor Slack user: {request.principal.slack_user_id}\n"
                            f"Actor roles: {[role.value for role in request.principal.roles]}\n"
                            f"Channel: {request.channel_id}\n"
                            f"Request ID: {request.request_id}\n"
                        ),
                    }
                ],
            }
        ]

    def _summarize_tool_results(self, results: list[ToolResult]) -> ToolResult:
        if len(results) == 1:
            return results[0]
        return ToolResult(
            ok=all(result.ok for result in results),
            message="MCP tools completed",
            data={"results": [result.data for result in results]},
        )

    def _safe_openai_error(self, exc: Exception, request_id: str) -> str:
        status_code = getattr(exc, "status_code", None)
        code = getattr(exc, "code", None)
        if not code:
            response = getattr(exc, "response", None)
            if response is not None:
                try:
                    payload = response.json()
                    if isinstance(payload, dict):
                        error = payload.get("error", {})
                        if isinstance(error, dict):
                            code = error.get("code") or error.get("type")
                except Exception:
                    code = None
        detail = f" status={status_code}" if status_code else ""
        reason = f" code={code}" if code else ""
        return f"OpenAI request failed{detail}{reason}. request_id={request_id}"

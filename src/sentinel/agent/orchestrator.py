from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sentinel.agent.mcp import SentinelMcpGateway
from sentinel.agent.tools import parse_tool_arguments
from sentinel.config import Settings
from sentinel.models import McpToolCall, OperationRequest, ToolResult


@dataclass(frozen=True)
class _SelectedTool:
    name: str
    arguments: Any


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
        self.provider = "gemini" if settings.gemini_api_key else "openai"
        api_key = settings.gemini_api_key or settings.openai_api_key
        base_url = settings.gemini_base_url if settings.gemini_api_key else None
        self.client = self._create_client(api_key, base_url)

    def _create_client(self, api_key: str | None, base_url: str | None) -> Any:
        if not api_key:
            return None
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise RuntimeError("openai is required for the configured LLM provider") from exc
        if base_url:
            return OpenAI(api_key=api_key, base_url=base_url)
        return OpenAI(api_key=api_key)

    def handle(self, request: OperationRequest) -> ToolResult:
        if not self.client:
            return ToolResult(
                False,
                "SENTINEL_GEMINI_API_KEY or SENTINEL_OPENAI_API_KEY is required. "
                "Sentinel no longer guesses MCP tools without the LLM.",
            )

        try:
            tool_calls = self._select_tool_calls(request)
        except Exception as exc:
            return ToolResult(False, self._safe_provider_error(exc, request.request_id))

        write_calls = [item for item in tool_calls if item.name in self.WRITE_TOOLS]
        if len(write_calls) > 1:
            names = ", ".join(item.name for item in write_calls)
            return ToolResult(False, f"LLM selected multiple write tools; refusing to execute: {names}")

        tool_results: list[ToolResult] = []
        for item in tool_calls:
            args = parse_tool_arguments(item.arguments)
            result = self.mcp.call_tool(request, McpToolCall(item.name, args))
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

    def _select_tool_calls(self, request: OperationRequest) -> list[_SelectedTool]:
        if self.provider == "gemini":
            response = self.client.chat.completions.create(
                model=self.settings.gemini_model,
                messages=[
                    {"role": "system", "content": self._instructions()},
                    {"role": "user", "content": self._input_text(request)},
                ],
                tools=self._chat_tool_schemas(),
                tool_choice="auto",
            )
            choices = getattr(response, "choices", [])
            if not choices:
                return []
            message = getattr(choices[0], "message", None)
            calls = getattr(message, "tool_calls", []) if message is not None else []
            return [
                _SelectedTool(
                    name=str(getattr(getattr(call, "function", None), "name", "")),
                    arguments=getattr(getattr(call, "function", None), "arguments", None),
                )
                for call in calls
                if getattr(getattr(call, "function", None), "name", None)
            ]

        response = self.client.responses.create(
            model=self.settings.openai_model,
            instructions=self._instructions(),
            input=self._input(request),
            tools=self.mcp.openai_tool_schemas(),
        )
        return [
            _SelectedTool(str(getattr(item, "name", "")), getattr(item, "arguments", None))
            for item in getattr(response, "output", [])
            if getattr(item, "type", None) == "function_call"
        ]

    def _chat_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": str(schema["name"]),
                    "description": str(schema.get("description", "")),
                    "parameters": dict(schema.get("parameters", {})),
                },
            }
            for schema in self.mcp.openai_tool_schemas()
        ]

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

    def _input_text(self, request: OperationRequest) -> str:
        return (
            f"Slack message: {request.text}\n"
            f"Actor Slack user: {request.principal.slack_user_id}\n"
            f"Actor roles: {[role.value for role in request.principal.roles]}\n"
            f"Channel: {request.channel_id}\n"
            f"Request ID: {request.request_id}\n"
        )

    def _summarize_tool_results(self, results: list[ToolResult]) -> ToolResult:
        if len(results) == 1:
            return results[0]
        return ToolResult(
            ok=all(result.ok for result in results),
            message="MCP tools completed",
            data={"results": [result.data for result in results]},
        )

    def _safe_provider_error(self, exc: Exception, request_id: str) -> str:
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
        provider = "Gemini" if self.provider == "gemini" else "OpenAI"
        return f"{provider} request failed{detail}{reason}. request_id={request_id}"

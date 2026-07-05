from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sentinel.models import McpToolCall, OperationRequest, ToolResult


@dataclass(frozen=True)
class McpToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


class SentinelMcpGateway:
    """In-process MCP-style gateway used by the LLM agent.

    The first implementation exposes local Python tools through the same shape an
    MCP server would expose: tool definitions plus tool calls. A later version can
    replace this with a real remote MCP client without changing the runtime flow.
    """

    def __init__(self, registry: Any) -> None:
        self.registry = registry

    def list_tools(self) -> list[McpToolDefinition]:
        definitions: list[McpToolDefinition] = []
        for schema in self.registry.schemas:
            definitions.append(
                McpToolDefinition(
                    name=str(schema["name"]),
                    description=str(schema.get("description", "")),
                    input_schema=dict(schema.get("parameters", {})),
                )
            )
        return definitions

    def openai_tool_schemas(self) -> list[dict[str, Any]]:
        return self.registry.schemas

    def call_tool(self, request: OperationRequest, call: McpToolCall) -> ToolResult:
        return self.registry.execute(request, call.name, call.arguments)

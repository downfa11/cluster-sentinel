from __future__ import annotations

import json
import logging
from typing import Any

from sentinel.models import OperationRequest, ToolResult


class AuditLogger:
    def __init__(self) -> None:
        self.logger = logging.getLogger("sentinel.audit")

    def write(
        self,
        event_type: str,
        request: OperationRequest | None,
        result: str,
        metadata: dict[str, Any] | None = None,
    ) -> ToolResult:
        payload = {
            "event_type": event_type,
            "result": result,
            "request_id": request.request_id if request else None,
            "actor": request.principal.slack_user_id if request else None,
            "command": request.command if request else None,
            "service": request.service if request else None,
            "environment": request.environment if request else None,
            "metadata": metadata or {},
        }
        self.logger.info(json.dumps(payload, sort_keys=True))
        return ToolResult(ok=True, message="audit event written", data=payload)

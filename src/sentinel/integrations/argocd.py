from __future__ import annotations

from typing import Any

from sentinel.config import Settings
from sentinel.models import OperationRequest, ToolResult


class ArgoCdClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def get_status(self, request: OperationRequest, args: dict[str, Any]) -> ToolResult:
        app_name = self._app_name(request, args)
        payload = self._get_json(f"/api/v1/applications/{app_name}")
        status = payload.get("status", {})
        sync = status.get("sync", {})
        health = status.get("health", {})
        return ToolResult(
            ok=True,
            message=f"{app_name}: health={health.get('status', 'unknown')} sync={sync.get('status', 'unknown')}",
            data={
                "application": app_name,
                "health": health.get("status"),
                "sync": sync.get("status"),
                "revision": sync.get("revision"),
                "conditions": status.get("conditions", []),
            },
        )

    def diff(self, request: OperationRequest, args: dict[str, Any]) -> ToolResult:
        app_name = self._app_name(request, args)
        payload = self._get_json(f"/api/v1/applications/{app_name}/managed-resources")
        items = payload.get("items", []) if isinstance(payload, dict) else []
        resources = [
            {
                "kind": item.get("kind"),
                "namespace": item.get("namespace"),
                "name": item.get("name"),
                "status": item.get("status"),
                "health": item.get("health", {}).get("status") if isinstance(item.get("health"), dict) else None,
            }
            for item in items
            if isinstance(item, dict)
        ]
        return ToolResult(ok=True, message=f"{app_name}: {len(resources)} managed resources", data={"application": app_name, "resources": resources})

    def _app_name(self, request: OperationRequest, args: dict[str, Any]) -> str:
        service = str(args.get("service") or request.service or "unknown")
        environment = str(args.get("environment") or request.environment or "unknown")
        return self.settings.argocd_app_name_template.format(service=service, environment=environment)

    def _get_json(self, path: str) -> dict[str, Any]:
        if not self.settings.argocd_base_url or not self.settings.argocd_token:
            raise RuntimeError("SENTINEL_ARGOCD_BASE_URL and SENTINEL_ARGOCD_TOKEN are required")
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("httpx is required for Argo CD API integration") from exc

        url = self.settings.argocd_base_url.rstrip("/") + path
        headers = {"Authorization": f"Bearer {self.settings.argocd_token}"}
        response = httpx.get(url, headers=headers, timeout=20.0)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {"items": payload}

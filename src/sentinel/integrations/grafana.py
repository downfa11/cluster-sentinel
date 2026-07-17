from __future__ import annotations

from typing import Any

from sentinel.config import Settings
from sentinel.models import OperationRequest, ToolResult


class GrafanaClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def alerts(self, request: OperationRequest, args: dict[str, Any]) -> ToolResult:
        service = str(args.get("_grafana_match") or args.get("service") or request.service or "")
        alerts = self._get_alerts()
        filtered = [
            alert for alert in alerts if not service or service.lower() in str(alert).lower()
        ]
        summaries = [self._summary(alert) for alert in filtered[:10]]
        message = f"Grafana alerts for {service}: {len(filtered)}"
        if summaries:
            message += "\n" + "\n".join(f"- {summary}" for summary in summaries)
        return ToolResult(
            ok=True,
            message=message,
            data={"service": service or None, "alerts": filtered[:20]},
        )

    def _summary(self, alert: dict[str, Any]) -> str:
        labels = alert.get("labels", {}) if isinstance(alert.get("labels"), dict) else {}
        annotations = (
            alert.get("annotations", {}) if isinstance(alert.get("annotations"), dict) else {}
        )
        name = labels.get("alertname") or labels.get("alert") or "unnamed alert"
        status = alert.get("status")
        if isinstance(status, dict):
            status = status.get("state")
        status = status or labels.get("status") or "unknown"
        summary = annotations.get("summary") or annotations.get("description") or ""
        summary = " ".join(str(summary).split())[:160]
        return f"{name} — {status}" + (f" — {summary}" if summary else "")

    def _get_alerts(self) -> list[dict[str, Any]]:
        if not self.settings.grafana_base_url or not self.settings.grafana_token:
            raise RuntimeError("SENTINEL_GRAFANA_BASE_URL and SENTINEL_GRAFANA_TOKEN are required")
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("httpx is required for Grafana API integration") from exc

        base_url = self.settings.grafana_base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {self.settings.grafana_token}"}
        with httpx.Client(timeout=20.0, headers=headers) as client:
            response = client.get(f"{base_url}/api/alertmanager/grafana/api/v2/alerts")
            if response.status_code == 404:
                response = client.get(f"{base_url}/api/alerts")
            response.raise_for_status()
            payload = response.json()
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict) and isinstance(payload.get("alerts"), list):
            return [item for item in payload["alerts"] if isinstance(item, dict)]
        return []

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from sentinel.config import Settings
from sentinel.models import OperationRequest, ToolResult


class ArgoCdClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def get_status(self, request: OperationRequest, args: dict[str, Any]) -> ToolResult:
        app_name = self._app_name(request, args)
        payload = self._get_json(f"/api/v1/applications/{quote(app_name, safe='')}")
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

    def list_applications(self, request: OperationRequest, args: dict[str, Any]) -> ToolResult:
        del request, args
        applications = self._allowed_applications()
        lines = [
            f"- {item['name']} — sync={item['sync']} health={item['health']}"
            for item in applications
        ]
        return ToolResult(
            ok=True,
            message=f"Allowed Argo CD applications: {len(applications)}"
            + (("\n" + "\n".join(lines)) if lines else ""),
            data={"applications": applications},
        )

    def list_out_of_sync(self, request: OperationRequest, args: dict[str, Any]) -> ToolResult:
        del request, args
        applications = [
            item for item in self._allowed_applications() if item["sync"] == "OutOfSync"
        ]
        lines = [f"- {item['name']} — health={item['health']}" for item in applications]
        return ToolResult(
            ok=True,
            message=f"OutOfSync applications: {len(applications)}"
            + (("\n" + "\n".join(lines)) if lines else ""),
            data={"applications": applications},
        )

    def diff(self, request: OperationRequest, args: dict[str, Any]) -> ToolResult:
        app_name = self._app_name(request, args)
        payload = self._get_json(
            f"/api/v1/applications/{quote(app_name, safe='')}/managed-resources"
        )
        items = payload.get("items", []) if isinstance(payload, dict) else []
        resources = [
            {
                "kind": item.get("kind"),
                "namespace": item.get("namespace"),
                "name": item.get("name"),
                "status": item.get("status"),
                "health": item.get("health", {}).get("status")
                if isinstance(item.get("health"), dict)
                else None,
            }
            for item in items
            if isinstance(item, dict)
        ]
        lines = [
            f"- {item.get('kind')}/{item.get('name')} — status={item.get('status') or 'unknown'} health={item.get('health') or 'unknown'}"
            for item in resources[:20]
        ]
        return ToolResult(
            ok=True,
            message=f"{app_name}: {len(resources)} managed resources"
            + (("\n" + "\n".join(lines)) if lines else ""),
            data={"application": app_name, "resources": resources},
        )

    def list_pods(self, request: OperationRequest, args: dict[str, Any]) -> ToolResult:
        app_name = self._app_name(request, args)
        pods = self._pods(app_name)
        lines = [
            f"- {item['namespace']}/{item['name']} — {item['status']} containers={','.join(item['containers']) or '-'}"
            for item in pods[:20]
        ]
        return ToolResult(
            ok=True,
            message=f"{app_name}: {len(pods)} pods" + (("\n" + "\n".join(lines)) if lines else ""),
            data={"application": app_name, "pods": pods},
        )

    def get_logs(self, request: OperationRequest, args: dict[str, Any]) -> ToolResult:
        app_name = self._app_name(request, args)
        pods = self._pods(app_name)
        requested_pod = str(args.get("pod") or "").strip()
        candidates = [pod for pod in pods if not requested_pod or pod["name"] == requested_pod]
        if not candidates:
            raise RuntimeError(f"pod is not managed by {app_name}: {requested_pod}")
        pod = next((item for item in candidates if item["status"] != "Running"), candidates[0])
        requested_container = str(args.get("container") or "").strip()
        containers = self._pod_containers(app_name, pod)
        if requested_container and containers and requested_container not in containers:
            raise RuntimeError(f"container is not part of {pod['name']}: {requested_container}")
        container = requested_container or (containers[0] if containers else "")
        tail_lines = min(max(int(args.get("tail_lines") or 100), 1), 500)
        path = f"/api/v1/applications/{quote(app_name, safe='')}/pods/{quote(pod['name'], safe='')}/logs"
        params = {"namespace": pod["namespace"], "tailLines": str(tail_lines)}
        if container:
            params["container"] = container
        logs = self._get_text(path, params)
        if len(logs) > 12000:
            logs = logs[-12000:]
        container_detail = f" container={container}" if container else ""
        rendered_logs = logs.rstrip() or "(no log lines)"
        return ToolResult(
            ok=True,
            message=(
                f"{app_name} {pod['namespace']}/{pod['name']}{container_detail} "
                f"(last {tail_lines} lines)"
            ),
            data={
                "application": app_name,
                "namespace": pod["namespace"],
                "pod": pod["name"],
                "container": container or None,
                "tail_lines": tail_lines,
                "slack_code_block": f"```\n{rendered_logs}\n```",
            },
        )

    def _allowed_applications(self) -> list[dict[str, str]]:
        allowed = {
            target["application"]
            for target in self.settings.operational_targets.values()
            if target.get("application")
        }
        if not allowed:
            raise RuntimeError("SENTINEL_OPERATIONAL_TARGETS is required for Argo CD listing")
        payload = self._get_json("/api/v1/applications")
        items = payload.get("items", [])
        applications: list[dict[str, str]] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("metadata", {}).get("name") or "")
            if name not in allowed:
                continue
            status = item.get("status", {})
            sync = status.get("sync", {}) if isinstance(status, dict) else {}
            health = status.get("health", {}) if isinstance(status, dict) else {}
            applications.append(
                {
                    "name": name,
                    "sync": str(sync.get("status") or "unknown"),
                    "health": str(health.get("status") or "unknown"),
                }
            )
        return sorted(applications, key=lambda item: item["name"])

    def _pods(self, app_name: str) -> list[dict[str, Any]]:
        payload = self._get_json(f"/api/v1/applications/{quote(app_name, safe='')}/resource-tree")
        items = payload.get("nodes", payload.get("items", []))
        is_resource_tree = "nodes" in payload
        pods: list[dict[str, Any]] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "")
            if is_resource_tree:
                group = str(item.get("group") or "")
                uid = str(item.get("uid") or item.get("metadata", {}).get("uid") or "")
                if kind != "Pod" or group not in {"", "core"} or not uid:
                    continue
            elif kind and kind != "Pod":
                continue
            name = str(item.get("name") or item.get("metadata", {}).get("name") or "")
            namespace = str(
                item.get("namespace") or item.get("metadata", {}).get("namespace") or ""
            )
            if not name or not namespace:
                continue
            pods.append(
                {
                    "name": name,
                    "namespace": namespace,
                    "status": self._pod_status(item),
                    "containers": self._container_names(item.get("containers", [])),
                }
            )
        return sorted(pods, key=lambda item: (item["namespace"], item["name"]))

    def _pod_containers(self, app_name: str, pod: dict[str, Any]) -> list[str]:
        known = self._container_names(pod.get("containers", []))
        if known:
            return known
        payload = self._get_json(
            f"/api/v1/applications/{quote(app_name, safe='')}/resource",
            {
                "namespace": str(pod["namespace"]),
                "resourceName": str(pod["name"]),
                "version": "v1",
                "kind": "Pod",
            },
        )
        manifest = payload.get("manifest")
        if isinstance(manifest, str):
            try:
                resource = json.loads(manifest)
            except json.JSONDecodeError:
                return []
        else:
            resource = manifest
        if not isinstance(resource, dict):
            return []
        spec = resource.get("spec")
        if not isinstance(spec, dict):
            return []
        return self._container_names(spec.get("containers", []))

    @staticmethod
    def _container_names(raw_containers: Any) -> list[str]:
        if not isinstance(raw_containers, list):
            return []
        names: list[str] = []
        for container in raw_containers:
            name = container.get("name") if isinstance(container, dict) else container
            if name:
                names.append(str(name))
        return names

    def _pod_status(self, pod: dict[str, Any]) -> str:
        info = pod.get("info", [])
        for item in info if isinstance(info, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("name") == "Status Reason" and item.get("value"):
                return str(item["value"])
        status = pod.get("status") or pod.get("phase")
        if status:
            return str(status)
        health = pod.get("health", {})
        if isinstance(health, dict) and health.get("status"):
            return str(health["status"])
        return "unknown"

    def _app_name(self, request: OperationRequest, args: dict[str, Any]) -> str:
        canonical = args.get("_application")
        if canonical:
            return str(canonical)
        service = str(args.get("service") or request.service or "unknown")
        environment = str(args.get("environment") or request.environment or "unknown")
        target = self.settings.gitops_targets.get(service)
        if target and environment == target.get("environment") and target.get("application"):
            return target["application"]
        return self.settings.argocd_app_name_template.format(
            service=service, environment=environment
        )

    def _get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        response = self._get(path, params=params)
        payload = response.json()
        return payload if isinstance(payload, dict) else {"items": payload}

    def _get_text(self, path: str, params: dict[str, str]) -> str:
        raw = str(self._get(path, params=params).text)
        return self._decode_log_stream(raw)

    @staticmethod
    def _decode_log_stream(raw: str) -> str:
        chunks: list[str] = []
        found_stream_entry = False
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                return raw
            result = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(result, dict):
                return raw
            found_stream_entry = True
            content = result.get("content")
            if isinstance(content, str):
                chunks.append(content)
        return "".join(chunks) if found_stream_entry else raw

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        if not self.settings.argocd_base_url or not self.settings.argocd_token:
            raise RuntimeError("SENTINEL_ARGOCD_BASE_URL and SENTINEL_ARGOCD_TOKEN are required")
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("httpx is required for Argo CD API integration") from exc
        url = self.settings.argocd_base_url.rstrip("/") + path
        headers = {"Authorization": f"Bearer {self.settings.argocd_token}"}
        response = httpx.get(url, headers=headers, params=params, timeout=20.0)
        response.raise_for_status()
        return response

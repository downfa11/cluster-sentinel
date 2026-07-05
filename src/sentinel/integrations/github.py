from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable

from sentinel.config import Settings
from sentinel.models import OperationRequest, ToolResult

RenderFile = Callable[[str | None], str]


@dataclass(frozen=True)
class FileMutation:
    path: str
    render: RenderFile


@dataclass(frozen=True)
class PullRequestDraft:
    title: str
    body: str
    mutations: list[FileMutation]
    labels: list[str]


class GitHubClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def create_pr(self, request: OperationRequest, draft: PullRequestDraft) -> ToolResult:
        if self.settings.github_pr_dry_run or not self.settings.github_token:
            return ToolResult(
                ok=True,
                message="dry-run PR created",
                data={
                    "dry_run": True,
                    "repo": self.settings.gitops_repo,
                    "title": draft.title,
                    "files": sorted(mutation.path for mutation in draft.mutations),
                    "labels": draft.labels,
                },
            )

        owner, repo = self.settings.gitops_repo.split("/", 1)
        action = draft.labels[1] if len(draft.labels) > 1 else "change"
        branch = f"sentinel/{action}-{request.request_id[:8]}"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.settings.github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        base_url = f"https://api.github.com/repos/{owner}/{repo}"

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise RuntimeError("httpx is required when GitHub dry-run is disabled") from exc

        with httpx.Client(timeout=30.0, headers=headers) as client:
            base_ref = client.get(f"{base_url}/git/ref/heads/{self.settings.github_default_branch}")
            base_ref.raise_for_status()
            sha = base_ref.json()["object"]["sha"]

            create_ref = client.post(f"{base_url}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha})
            create_ref.raise_for_status()

            for mutation in draft.mutations:
                self._mutate_file(client, base_url, branch, mutation, draft.title)

            pr = client.post(
                f"{base_url}/pulls",
                json={
                    "title": draft.title,
                    "head": branch,
                    "base": self.settings.github_default_branch,
                    "body": draft.body,
                    "maintainer_can_modify": True,
                },
            )
            pr.raise_for_status()
            pr_payload = pr.json()

            if draft.labels:
                client.post(f"{base_url}/issues/{pr_payload['number']}/labels", json={"labels": draft.labels}).raise_for_status()

        return ToolResult(ok=True, message="pull request created", data={"pull_request_url": pr_payload["html_url"], "branch": branch})

    def _mutate_file(self, client: Any, base_url: str, branch: str, mutation: FileMutation, message: str) -> None:
        encoded_path = str(PurePosixPath(mutation.path))
        existing = client.get(f"{base_url}/contents/{encoded_path}", params={"ref": branch})
        sha = None
        current_content = None
        if existing.status_code == 200:
            payload = existing.json()
            sha = payload.get("sha")
            encoded = str(payload.get("content", "")).replace("\n", "")
            current_content = base64.b64decode(encoded).decode("utf-8") if encoded else ""
        elif existing.status_code == 404:
            current_content = None
        else:
            existing.raise_for_status()
        new_content = mutation.render(current_content)
        request_payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            request_payload["sha"] = sha
        response = client.put(f"{base_url}/contents/{encoded_path}", json=request_payload)
        response.raise_for_status()


class GitOpsPullRequestFactory:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def deploy(self, request: OperationRequest, args: dict[str, Any]) -> PullRequestDraft:
        service = str(args.get("service") or request.service or "unknown")
        environment = str(args.get("environment") or request.environment or "staging")
        image_tag = str(args.get("image_tag") or args.get("target") or "latest")
        path = self.settings.gitops_deploy_values_path_template.format(service=service, environment=environment)
        return PullRequestDraft(
            title=f"sentinel: deploy {service} to {environment}",
            body=self._body(request, "deploy", args),
            mutations=[FileMutation(path=path, render=lambda current: self._render_image_values(current, service, environment, image_tag, request))],
            labels=["sentinel", "deploy", f"env:{environment}"],
        )

    def restart(self, request: OperationRequest, args: dict[str, Any]) -> PullRequestDraft:
        service = str(args.get("service") or request.service or "unknown")
        environment = str(args.get("environment") or request.environment or "staging")
        path = self.settings.gitops_deploy_values_path_template.format(service=service, environment=environment)
        return PullRequestDraft(
            title=f"sentinel: restart {service} in {environment}",
            body=self._body(request, "restart", args),
            mutations=[FileMutation(path=path, render=lambda current: self._render_restart_values(current, service, environment, request))],
            labels=["sentinel", "restart", f"env:{environment}"],
        )

    def rollback(self, request: OperationRequest, args: dict[str, Any]) -> PullRequestDraft:
        target = str(args.get("target") or args.get("image_tag") or "previous")
        args = {**args, "image_tag": target}
        draft = self.deploy(request, args)
        return PullRequestDraft(
            title=draft.title.replace("deploy", "rollback"),
            body=self._body(request, "rollback", args),
            mutations=draft.mutations,
            labels=[label.replace("deploy", "rollback") for label in draft.labels],
        )

    def access_change(self, request: OperationRequest, args: dict[str, Any]) -> PullRequestDraft:
        user = str(args.get("user") or "unknown@example.com")
        action = str(args.get("action") or "access")
        path = "access/users.yaml"
        return PullRequestDraft(
            title=f"sentinel: {action} {user}",
            body=self._body(request, action, args),
            mutations=[FileMutation(path=path, render=lambda current: self._render_access_users(current, request, args))],
            labels=["sentinel", "access"],
        )

    def _render_image_values(self, current: str | None, service: str, environment: str, image_tag: str, request: OperationRequest) -> str:
        values = self._load_yaml(current)
        repository, tag = self._split_image(image_tag)
        image = values.setdefault("image", {})
        if not isinstance(image, dict):
            image = {}
            values["image"] = image
        if repository:
            image["repository"] = repository
        image["tag"] = tag
        values.setdefault("sentinel", {})
        values["sentinel"].update({"lastRequestId": request.request_id, "service": service, "environment": environment})
        return self._dump_yaml(values)

    def _render_restart_values(self, current: str | None, service: str, environment: str, request: OperationRequest) -> str:
        values = self._load_yaml(current)
        annotations = values.setdefault("podAnnotations", {})
        if not isinstance(annotations, dict):
            annotations = {}
            values["podAnnotations"] = annotations
        annotations["sentinel.dev/restartedAt"] = request.request_id
        values.setdefault("sentinel", {})
        values["sentinel"].update({"lastRequestId": request.request_id, "service": service, "environment": environment})
        return self._dump_yaml(values)

    def _render_access_users(self, current: str | None, request: OperationRequest, args: dict[str, Any]) -> str:
        values = self._load_yaml(current)
        raw_users = values.setdefault("users", [])
        if not isinstance(raw_users, list):
            raise RuntimeError("access/users.yaml must contain a users list")

        action = str(args.get("action") or "access")
        user_id = str(args.get("user") or "").strip()
        if not user_id:
            raise RuntimeError("access change requires user")

        user = self._find_or_create_access_user(raw_users, user_id, action)
        user["email"] = str(args.get("email") or user.get("email") or user_id)
        user["status"] = "active"
        for field in ("github_username", "slack_user_id"):
            if args.get(field):
                user[field] = str(args[field])

        if action == "offboard":
            user["status"] = "inactive"
            user["roles"] = []
            user["groups"] = []
        elif action == "grant":
            self._require_role_or_group(action, args)
            self._add_access_value(user, "roles", args.get("role"))
            self._add_access_value(user, "groups", args.get("group"))
        elif action == "revoke":
            self._require_role_or_group(action, args)
            self._remove_access_value(user, "roles", args.get("role"))
            self._remove_access_value(user, "groups", args.get("group"))
        elif action == "onboard":
            self._add_access_value(user, "roles", args.get("role"))
            self._add_access_value(user, "groups", args.get("group"))
        else:
            raise RuntimeError(f"unsupported access action: {action}")

        values["sentinel"] = {
            "lastRequestId": request.request_id,
            "lastActorSlackUserId": request.principal.slack_user_id,
            "lastAccessAction": action,
        }
        return self._dump_yaml(values)

    def _find_or_create_access_user(self, users: list[Any], user_id: str, action: str) -> dict[str, Any]:
        for item in users:
            if not isinstance(item, dict):
                continue
            identifiers = {str(item.get("email") or ""), str(item.get("id") or ""), str(item.get("slack_user_id") or "")}
            if user_id in identifiers:
                return item
        if action in {"offboard", "revoke"}:
            raise RuntimeError(f"access user not found: {user_id}")
        created: dict[str, Any] = {"email": user_id, "status": "active", "roles": [], "groups": []}
        users.append(created)
        return created

    def _require_role_or_group(self, action: str, args: dict[str, Any]) -> None:
        role = args.get("role")
        group = args.get("group")
        has_role = role is not None and (not isinstance(role, str) or bool(role.strip()))
        has_group = group is not None and (not isinstance(group, str) or bool(group.strip()))
        if not has_role and not has_group:
            raise RuntimeError(f"access {action} requires role or group")

    def _add_access_value(self, user: dict[str, Any], field: str, value: Any) -> None:
        if value is None or str(value).strip() == "":
            return
        items = [str(item) for item in user.get(field, [])]
        candidate = str(value)
        if candidate not in items:
            items.append(candidate)
        user[field] = sorted(items)

    def _remove_access_value(self, user: dict[str, Any], field: str, value: Any) -> None:
        if value is None or str(value).strip() == "":
            return
        candidate = str(value)
        user[field] = sorted(str(item) for item in user.get(field, []) if str(item) != candidate)

    def _load_yaml(self, current: str | None) -> dict[str, Any]:
        if not current or not current.strip():
            return {}
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("PyYAML is required to patch GitOps values files") from exc
        loaded = yaml.safe_load(current) or {}
        if not isinstance(loaded, dict):
            raise RuntimeError("GitOps values file must contain a YAML mapping")
        return loaded

    def _dump_yaml(self, values: dict[str, Any]) -> str:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("PyYAML is required to patch GitOps values files") from exc
        return yaml.safe_dump(values, sort_keys=False, allow_unicode=True)

    def _split_image(self, image_tag: str) -> tuple[str | None, str]:
        if ":" not in image_tag:
            return None, image_tag
        repository, tag = image_tag.rsplit(":", 1)
        return repository, tag

    def _header(self, request: OperationRequest) -> str:
        return "# Generated by Sentinel. Review before merge.\n" f"requestId: {request.request_id}\n" f"actorSlackUserId: {request.principal.slack_user_id}\n"

    def _body(self, request: OperationRequest, action: str, args: dict[str, Any]) -> str:
        return "\n".join(
            [
                "Generated by Sentinel.",
                "",
                f"- Request ID: `{request.request_id}`",
                f"- Actor: `{request.principal.slack_user_id}`",
                f"- Action: `{action}`",
                f"- Service: `{args.get('service') or request.service or 'n/a'}`",
                f"- Environment: `{args.get('environment') or request.environment or 'n/a'}`",
                "",
                "This PR must be reviewed and merged by a human before any system changes occur.",
            ]
        )


from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable

import hjson

from sentinel.config import Settings
from sentinel.models import OperationRequest, ToolResult

RenderFile = Callable[[str | None], str]
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_NAME = re.compile(r"[^a-z0-9-]+")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ACCESS_ROLES = {"gui-user", "dev", "operator", "admin"}


@dataclass(frozen=True)
class FileMutation:
    path: str
    render: RenderFile


@dataclass(frozen=True)
class PullRequestDraft:
    action: str
    title: str
    body: str
    mutations: list[FileMutation]
    idempotency_key: str | None = None


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
                },
            )
        if not self.settings.github_commit_signoff:
            raise RuntimeError("SENTINEL_GITHUB_COMMIT_SIGNOFF is required for live pull requests")

        owner, repo = self.settings.gitops_repo.split("/", 1)
        action = _SAFE_NAME.sub("-", draft.action.lower()).strip("-") or "change"
        suffix = draft.idempotency_key or request.request_id[:8].lower()
        suffix = _SAFE_NAME.sub("-", suffix.lower()).strip("-")
        branch = f"fix/sentinel-{action}-{suffix}"
        base_url = f"https://api.github.com/repos/{owner}/{repo}"

        with self._http_client() as client:
            existing = self._open_pull_request(client, base_url, owner, branch)
            if existing:
                return ToolResult(
                    ok=True,
                    message="draft pull request already pending",
                    data={
                        "pull_request_url": existing["html_url"],
                        "branch": branch,
                        "already_pending": True,
                    },
                )
            base_ref = client.get(f"{base_url}/git/ref/heads/{self.settings.github_default_branch}")
            base_ref.raise_for_status()
            sha = base_ref.json()["object"]["sha"]
            created = client.post(
                f"{base_url}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha}
            )
            created.raise_for_status()

            try:
                commit_message = (
                    f"{draft.title}\n\nSigned-off-by: {self.settings.github_commit_signoff}"
                )
                for mutation in draft.mutations:
                    self._mutate_file(client, base_url, branch, mutation, commit_message)

                pr = client.post(
                    f"{base_url}/pulls",
                    json={
                        "title": draft.title,
                        "head": branch,
                        "base": self.settings.github_default_branch,
                        "body": draft.body,
                        "maintainer_can_modify": True,
                        "draft": True,
                    },
                )
                pr.raise_for_status()
                payload = pr.json()
            except Exception:
                try:
                    cleanup = client.delete(f"{base_url}/git/refs/heads/{branch}")
                    if cleanup.status_code not in {204, 404}:
                        cleanup.raise_for_status()
                except Exception:
                    pass
                raise

        return ToolResult(
            ok=True,
            message="draft pull request created",
            data={"pull_request_url": payload["html_url"], "branch": branch},
        )

    def _open_pull_request(
        self, client: Any, base_url: str, owner: str, branch: str
    ) -> dict[str, Any] | None:
        response = client.get(
            f"{base_url}/pulls",
            params={
                "state": "open",
                "head": f"{owner}:{branch}",
                "base": self.settings.github_default_branch,
                "per_page": 1,
            },
        )
        response.raise_for_status()
        pulls = response.json()
        if not isinstance(pulls, list) or not pulls:
            return None
        first = pulls[0]
        return first if isinstance(first, dict) else None

    def read_file(self, path: str) -> str:
        if not self.settings.github_token:
            raise RuntimeError("SENTINEL_GITHUB_TOKEN is required for access lookup")
        owner, repo = self.settings.gitops_repo.split("/", 1)
        encoded_path = str(PurePosixPath(path))
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{encoded_path}"
        with self._http_client() as client:
            response = client.get(url, params={"ref": self.settings.github_default_branch})
            response.raise_for_status()
            encoded = str(response.json().get("content", "")).replace("\n", "")
        return base64.b64decode(encoded).decode("utf-8")

    def _http_client(self) -> Any:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("httpx is required for GitHub integration") from exc
        return httpx.Client(
            timeout=30.0,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.settings.github_token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def _mutate_file(
        self,
        client: Any,
        base_url: str,
        branch: str,
        mutation: FileMutation,
        message: str,
    ) -> None:
        encoded_path = str(PurePosixPath(mutation.path))
        existing = client.get(f"{base_url}/contents/{encoded_path}", params={"ref": branch})
        if existing.status_code != 200:
            existing.raise_for_status()
        payload = existing.json()
        encoded = str(payload.get("content", "")).replace("\n", "")
        current = base64.b64decode(encoded).decode("utf-8")
        new_content = mutation.render(current)
        if new_content == current:
            raise RuntimeError(f"requested change is already present in {mutation.path}")
        response = client.put(
            f"{base_url}/contents/{encoded_path}",
            json={
                "message": message,
                "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
                "branch": branch,
                "sha": payload["sha"],
            },
        )
        response.raise_for_status()


class GitOpsPullRequestFactory:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def deploy(self, request: OperationRequest, args: dict[str, Any]) -> PullRequestDraft:
        service, environment, target = self._target(request, args)
        image = self._normalize_image(target, str(args.get("image_tag") or ""))
        return PullRequestDraft(
            action="deploy",
            title=f"deploy: update {service} image",
            body=self._body(request, "deploy", service, environment),
            mutations=[
                FileMutation(
                    target["path"], lambda current: self._render_image(current, target, image)
                )
            ],
        )

    def restart(self, request: OperationRequest, args: dict[str, Any]) -> PullRequestDraft:
        service, environment, target = self._target(request, args)
        return PullRequestDraft(
            action="restart",
            title=f"chore: restart {service}",
            body=self._body(request, "restart", service, environment),
            mutations=[
                FileMutation(
                    target["path"],
                    lambda current: self._render_restart(current, request.request_id),
                )
            ],
        )

    def rollback(self, request: OperationRequest, args: dict[str, Any]) -> PullRequestDraft:
        service, environment, target = self._target(request, args)
        image = self._normalize_image(target, str(args.get("target") or ""))
        return PullRequestDraft(
            action="rollback",
            title=f"revert: roll back {service} image",
            body=self._body(request, "rollback", service, environment),
            mutations=[
                FileMutation(
                    target["path"], lambda current: self._render_image(current, target, image)
                )
            ],
        )

    def access_change(self, request: OperationRequest, args: dict[str, Any]) -> PullRequestDraft:
        user = str(args.get("user") or "").strip()
        action = str(args.get("action") or "").strip()
        if not user or action not in {"onboard", "offboard", "grant", "revoke"}:
            raise RuntimeError("access change requires a supported action and user")
        if not _EMAIL.fullmatch(user):
            raise RuntimeError("access user must be an email address")
        idempotency_key = None
        if action == "onboard":
            idempotency_key = hashlib.sha256(user.lower().encode("utf-8")).hexdigest()[:12]
        return PullRequestDraft(
            action=f"access-{action}",
            title=f"access: {action} {user}",
            body=self._body(request, action, "access", "production"),
            mutations=[
                FileMutation(
                    "access/users.yaml",
                    lambda current: self._render_access_users(current, args),
                ),
                FileMutation(
                    "external/tailscale/policy.hujson",
                    lambda current: self._render_tailscale_policy(current, args),
                ),
            ],
            idempotency_key=idempotency_key,
        )

    def find_access_user(self, current: str, identifier: str) -> dict[str, str] | None:
        values = self._load_yaml(current, "access/users.yaml")
        users = values.get("users", [])
        if not isinstance(users, list):
            raise RuntimeError("access/users.yaml must contain a users list")
        for item in users:
            if not isinstance(item, dict):
                continue
            ids = {str(item.get(key) or "") for key in ("id", "email", "slack", "github")}
            if identifier in ids:
                return {str(key): str(value) for key, value in item.items() if value is not None}
        return None

    def application_name(self, request: OperationRequest, args: dict[str, Any]) -> str:
        try:
            _, _, target = self._target(request, args)
        except RuntimeError:
            service = str(args.get("service") or request.service or "unknown")
            environment = str(args.get("environment") or request.environment or "unknown")
            return self.settings.argocd_app_name_template.format(
                service=service, environment=environment
            )
        return target["application"]

    def _target(
        self,
        request: OperationRequest,
        args: dict[str, Any],
    ) -> tuple[str, str, dict[str, str]]:
        service = str(args.get("service") or request.service or "").strip()
        environment = str(args.get("environment") or request.environment or "").strip()
        target = self.settings.gitops_targets.get(service)
        if not target:
            raise RuntimeError(f"unsupported GitOps service: {service}")
        required = {"path", "repository", "application", "environment"}
        missing = sorted(required - target.keys())
        if missing:
            raise RuntimeError(f"GitOps target {service} is missing: {', '.join(missing)}")
        if environment != target["environment"]:
            raise RuntimeError(f"unsupported environment for {service}: {environment}")
        path = PurePosixPath(target["path"])
        if path.is_absolute() or ".." in path.parts or path.suffix not in {".yaml", ".yml"}:
            raise RuntimeError(f"unsafe GitOps target path: {target['path']}")
        return service, environment, target

    def _normalize_image(self, target: dict[str, str], requested: str) -> str:
        repository = target["repository"]
        digest = requested
        if requested.startswith(repository + "@"):
            digest = requested[len(repository) + 1 :]
        elif "@" in requested or requested.startswith("ghcr.io/"):
            raise RuntimeError("requested image repository does not match the configured target")
        if not _DIGEST.fullmatch(digest):
            raise RuntimeError("image must use an immutable sha256 digest")
        return f"{repository}@{digest}"

    def _render_image(self, current: str | None, target: dict[str, str], image: str) -> str:
        if current is None:
            raise RuntimeError(f"GitOps manifest does not exist: {target['path']}")
        repository = re.escape(target["repository"])
        pattern = re.compile(
            rf"(?m)^(\s*image:\s*)(?P<quote>['\"]?){repository}"
            rf"@sha256:[0-9a-f]{{64}}(?P=quote)(?P<suffix>\s*(?:#.*)?)$"
        )

        def replace(match: re.Match[str]) -> str:
            quote = match.group("quote")
            return f"{match.group(1)}{quote}{image}{quote}{match.group('suffix')}"

        rendered, count = pattern.subn(replace, current)
        if count != 1:
            raise RuntimeError(
                f"expected exactly one digest-pinned image for {target['repository']}, found {count}"
            )
        return rendered

    def _render_restart(self, current: str | None, request_id: str) -> str:
        if current is None:
            raise RuntimeError("GitOps manifest does not exist")
        annotation = "sentinel.dev/restartedAt"
        existing = re.compile(rf"(?m)^(\s*){re.escape(annotation)}:\s*.*$")
        if existing.search(current):
            return existing.sub(rf'\g<1>{annotation}: "{request_id}"', current, count=1)
        annotations = re.compile(r"(?m)^(\s{6}annotations:\s*(?:#.*)?)$")
        if annotations.search(current):
            return annotations.sub(rf'\1\n        {annotation}: "{request_id}"', current, count=1)
        marker = re.compile(r"(?m)^(\s{6}labels:\s*.*)$")
        rendered, count = marker.subn(
            rf'\1\n      annotations:\n        {annotation}: "{request_id}"', current, count=1
        )
        if count != 1:
            raise RuntimeError("could not locate Deployment pod-template metadata")
        return rendered

    def _render_access_users(self, current: str | None, args: dict[str, Any]) -> str:
        values = self._load_yaml(current, "access/users.yaml")
        users = values.setdefault("users", [])
        if not isinstance(users, list):
            raise RuntimeError("access/users.yaml must contain a users list")
        action = str(args["action"])
        identifier = str(args["user"]).strip()
        user = self._find_or_create_user(users, identifier, action)

        if action == "onboard":
            user["id"] = str(args.get("id") or user.get("id") or self._user_id(identifier))
            user["name"] = str(args.get("name") or user.get("name") or user["id"])
            user["email"] = str(args.get("email") or user.get("email") or identifier)
            user["role"] = self._role(args.get("role") or user.get("role") or "gui-user")
            user["status"] = "active"
        elif action == "offboard":
            user["status"] = "inactive"
        elif action == "grant":
            user["role"] = self._role(args.get("role"))
            user["status"] = "active"
        elif action == "revoke":
            requested = self._role(args.get("role"))
            if str(user.get("role")) != requested:
                raise RuntimeError(f"user does not have role {requested}")
            user["role"] = "gui-user"

        if args.get("github") or args.get("github_username"):
            user["github"] = str(args.get("github") or args.get("github_username"))
        if args.get("slack") or args.get("slack_user_id"):
            user["slack"] = str(args.get("slack") or args.get("slack_user_id"))
        return self._dump_yaml(values)

    def _render_tailscale_policy(self, current: str | None, args: dict[str, Any]) -> str:
        if not current or not current.strip():
            raise RuntimeError("external/tailscale/policy.hujson does not exist or is empty")
        try:
            policy = hjson.loads(current)
        except ValueError as exc:
            raise RuntimeError("external/tailscale/policy.hujson must be valid HuJSON") from exc
        groups = policy.get("groups")
        if not isinstance(groups, dict):
            raise RuntimeError("Tailscale policy must contain a groups mapping")
        role_groups = {
            role: str(metadata.get("tailscale_group") or "")
            for role, metadata in self.settings.access_role_groups.items()
            if isinstance(metadata, dict)
        }
        if set(role_groups) != _ACCESS_ROLES or any(not value for value in role_groups.values()):
            raise RuntimeError("SENTINEL_ACCESS_ROLE_GROUPS must map every access role")
        email = str(args["user"]).strip()
        desired: dict[str, list[str]] = {}
        for group_name in role_groups.values():
            members = groups.get(group_name)
            if not isinstance(members, list):
                raise RuntimeError(f"managed Tailscale group is missing: {group_name}")
            desired[group_name] = sorted({str(member) for member in members if member != email})
        action = str(args["action"])
        if action != "offboard":
            role = "gui-user" if action == "revoke" else self._role(args.get("role") or "gui-user")
            group_name = role_groups[role]
            desired[group_name] = sorted({*desired[group_name], email})

        rendered = current
        for group_name, members in desired.items():
            rendered = self._replace_hujson_group(rendered, group_name, members)
        return rendered if rendered.endswith("\n") else rendered + "\n"

    def _replace_hujson_group(self, current: str, group_name: str, members: list[str]) -> str:
        key = re.escape(json.dumps(group_name))
        match = re.search(rf"(?P<indent>[ \t]*){key}\s*:\s*\[", current)
        if not match:
            raise RuntimeError(f"managed Tailscale group is missing from HuJSON: {group_name}")
        start = match.end() - 1
        end = self._hujson_array_end(current, start)
        inner = current[start + 1 : end]
        comments = re.findall(r"//[^\r\n]*|/\*.*?\*/", inner, flags=re.DOTALL)
        multiline = "\n" in inner or bool(comments)
        if multiline:
            item_indent = match.group("indent") + "  "
            lines = [f"{item_indent}{comment.strip()}" for comment in comments]
            lines.extend(f"{item_indent}{json.dumps(member)}," for member in members)
            replacement = "\n" + "\n".join(lines) + "\n" + match.group("indent")
        else:
            replacement = ", ".join(json.dumps(member) for member in members)
        return current[: start + 1] + replacement + current[end:]

    def _hujson_array_end(self, current: str, start: int) -> int:
        quote: str | None = None
        escaped = False
        line_comment = False
        block_comment = False
        index = start + 1
        while index < len(current):
            char = current[index]
            following = current[index + 1] if index + 1 < len(current) else ""
            if line_comment:
                line_comment = char not in "\r\n"
            elif block_comment:
                if char == "*" and following == "/":
                    block_comment = False
                    index += 1
            elif quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
            elif char in {"'", '"'}:
                quote = char
            elif char == "/" and following == "/":
                line_comment = True
                index += 1
            elif char == "/" and following == "*":
                block_comment = True
                index += 1
            elif char == "]":
                return index
            index += 1
        raise RuntimeError("unterminated managed Tailscale group array")

    def _find_or_create_user(
        self, users: list[Any], identifier: str, action: str
    ) -> dict[str, Any]:
        for item in users:
            if not isinstance(item, dict):
                continue
            ids = {str(item.get(key) or "") for key in ("id", "email", "slack", "github")}
            if identifier in ids:
                return item
        if action != "onboard":
            raise RuntimeError(f"access user not found: {identifier}")
        created: dict[str, Any] = {}
        users.append(created)
        return created

    def _role(self, value: Any) -> str:
        role = str(value or "").strip()
        if role not in _ACCESS_ROLES:
            raise RuntimeError(f"unsupported access role: {role}")
        return role

    def _user_id(self, identifier: str) -> str:
        candidate = identifier.split("@", 1)[0].lower()
        candidate = _SAFE_NAME.sub("-", candidate).strip("-")
        if not candidate:
            raise RuntimeError("could not derive a user id")
        return candidate

    def _load_yaml(self, current: str | None, path: str) -> dict[str, Any]:
        if not current or not current.strip():
            raise RuntimeError(f"GitOps file does not exist or is empty: {path}")
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("PyYAML is required for GitOps changes") from exc
        loaded = yaml.safe_load(current)
        if not isinstance(loaded, dict):
            raise RuntimeError(f"{path} must contain a YAML mapping")
        return loaded

    def _dump_yaml(self, values: dict[str, Any]) -> str:
        import yaml

        return str(yaml.safe_dump(values, sort_keys=False, allow_unicode=True))

    def _body(
        self,
        request: OperationRequest,
        action: str,
        service: str,
        environment: str,
    ) -> str:
        kubernetes = action in {"deploy", "restart", "rollback"}
        access = action in {"onboard", "offboard", "grant", "revoke"}
        return "\n".join(
            [
                "## Summary",
                "",
                f"- Sentinel-generated `{action}` change for `{service}` in `{environment}`.",
                f"- Request ID: `{request.request_id}`",
                f"- Actor Slack user: `{request.principal.slack_user_id}`",
                "- Requires human review and merge; Sentinel never merges pull requests.",
                "",
                "## Change Type",
                "",
                f"- [{'x' if kubernetes else ' '}] Kubernetes desired state",
                "- [ ] External configuration declaration",
                f"- [{'x' if access else ' '}] Access change",
                "- [ ] Documentation",
                "- [x] Bot-generated request",
                "",
                "## Checks",
                "",
                "- [x] No plaintext secrets are included",
                "- [x] Access impact is understood",
                "- [x] Rollback or recovery path is documented",
                "- [x] Required external manual steps are listed",
                "",
                "## External Apply Steps",
                "",
                (
                    "- The cluster-config access-sync workflow publishes the reviewed Tailscale policy."
                    if access
                    else "- None. Argo CD applies the merged GitOps change."
                ),
            ]
        )

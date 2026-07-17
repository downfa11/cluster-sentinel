import base64
import json
from pathlib import Path
from typing import Any

import pytest

from sentinel.access.sync import AccessDirectory, AccessSync
from sentinel.agent.tools import ToolRegistry
from sentinel.audit import AuditLogger
from sentinel.config import Settings
from sentinel.identity import IdentityResolver
from sentinel.integrations.argocd import ArgoCdClient
from sentinel.integrations.github import (
    FileMutation,
    GitHubClient,
    GitOpsPullRequestFactory,
    PullRequestDraft,
)
from sentinel.integrations.grafana import GrafanaClient
from sentinel.models import OperationRequest, Principal, Role, ToolResult
from sentinel.policy import PolicyEngine


def _request(roles: set[Role] | None = None) -> OperationRequest:
    return OperationRequest(
        request_id="abcdef123456",
        channel_id="C1",
        text="test",
        principal=Principal("U1", "U1", None, roles or {Role.ADMIN}),
    )


def _access_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "access_role_groups": {
            "gui-user": {"tailscale_group": "group:gui-users"},
            "dev": {"tailscale_group": "group:dev"},
            "operator": {"tailscale_group": "group:operator"},
            "admin": {"tailscale_group": "group:admin"},
        }
    }
    values.update(overrides)
    return Settings(**values)


def test_unknown_slack_actor_is_fail_closed() -> None:
    principal = IdentityResolver(Settings()).resolve_slack_user("U-UNKNOWN")
    assert principal.roles == set()
    assert (
        not PolicyEngine().authorize_request(OperationRequest("r", "C", "hello", principal)).allowed
    )


def test_access_sync_preserves_unmanaged_policy_and_requires_base(tmp_path: Path) -> None:
    access = tmp_path / "access"
    access.mkdir()
    (access / "users.yaml").write_text(
        "users:\n  - email: alice@example.com\n    role: dev\n    status: active\n",
        encoding="utf-8",
    )
    (access / "roles.yaml").write_text(
        "roles:\n  dev:\n    tailscale_group: group:dev\n",
        encoding="utf-8",
    )
    sync = AccessSync(AccessDirectory(access), dry_run=True)
    existing = {
        "groups": {"group:dev": [], "group:commerce-db-dev": ["db@example.com"]},
        "grants": [{"src": ["group:commerce-db-dev"], "dst": ["tag:mysql-dev"]}],
    }
    rendered = sync.render_tailscale_policy(existing)
    assert rendered["groups"]["group:dev"] == ["alice@example.com"]
    assert rendered["groups"]["group:commerce-db-dev"] == ["db@example.com"]
    assert rendered["grants"] == existing["grants"]
    with pytest.raises(RuntimeError, match="existing Tailscale policy"):
        sync.render_tailscale_policy(None)


def test_access_pr_updates_users_and_policy_without_touching_db_group() -> None:
    factory = GitOpsPullRequestFactory(_access_settings())
    draft = factory.access_change(
        _request(), {"action": "grant", "user": "alice@example.com", "role": "operator"}
    )
    assert [mutation.path for mutation in draft.mutations] == [
        "access/users.yaml",
        "external/tailscale/policy.hujson",
    ]
    policy = {
        "groups": {
            "group:gui-users": ["alice@example.com"],
            "group:dev": [],
            "group:operator": [],
            "group:admin": [],
            "group:commerce-db-dev": ["alice@example.com"],
        },
        "grants": [{"src": ["group:commerce-db-dev"]}],
    }
    rendered = json.loads(draft.mutations[1].render(json.dumps(policy)))
    assert rendered["groups"]["group:operator"] == ["alice@example.com"]
    assert rendered["groups"]["group:gui-users"] == []
    assert rendered["groups"]["group:commerce-db-dev"] == ["alice@example.com"]
    assert rendered["grants"] == policy["grants"]


class _CapturingArgo:
    def __init__(self) -> None:
        self.args: dict[str, Any] | None = None

    def get_status(self, _request: OperationRequest, args: dict[str, Any]) -> ToolResult:
        self.args = args
        return ToolResult(True, "ok")

    diff = get_status
    list_pods = get_status
    get_logs = get_status
    list_applications = get_status
    list_out_of_sync = get_status


def test_operational_environment_is_server_side_and_cannot_be_spoofed() -> None:
    settings = Settings(
        operational_targets={
            "commerce": {
                "application": "commerce",
                "environment": "production",
                "grafana_match": "commerce",
            }
        }
    )
    argo = _CapturingArgo()
    registry = ToolRegistry(
        PolicyEngine(), GitHubClient(settings), AuditLogger(), argo, GrafanaClient(settings)
    )
    denied = registry.execute(
        _request({Role.DEV}),
        "argocd_get_status",
        {"service": "commerce", "environment": "dev"},
    )
    assert not denied.ok
    assert "production" in denied.message
    allowed = registry.execute(
        _request({Role.OPERATOR}),
        "argocd_get_status",
        {"service": "commerce", "environment": "dev"},
    )
    assert allowed.ok
    assert argo.args is not None
    assert argo.args["environment"] == "production"
    assert argo.args["_application"] == "commerce"


class _FakeResponse:
    def __init__(self, status: int = 200, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHttpClient:
    def __init__(self, fail_put: bool = False) -> None:
        self.fail_put = fail_put
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __enter__(self) -> "_FakeHttpClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(("GET", url, kwargs))
        if "/git/ref/heads/" in url:
            return _FakeResponse(payload={"object": {"sha": "base-sha"}})
        content = base64.b64encode(b"old\n").decode()
        return _FakeResponse(payload={"sha": "file-sha", "content": content})

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/pulls"):
            return _FakeResponse(payload={"html_url": "https://example.test/pr/1"})
        return _FakeResponse(status=201)

    def put(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(("PUT", url, kwargs))
        return _FakeResponse(status=500 if self.fail_put else 200)

    def delete(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(("DELETE", url, kwargs))
        return _FakeResponse(status=204)


def test_live_github_pr_is_draft_signed_off_and_cleans_failed_branch() -> None:
    settings = _access_settings(
        github_token="token",
        github_pr_dry_run=False,
        github_commit_signoff="Sentinel <sentinel@example.com>",
        gitops_repo="owner/repo",
    )
    draft = PullRequestDraft(
        "test", "chore: test", "body", [FileMutation("file.yaml", lambda _old: "new\n")]
    )
    client = GitHubClient(settings)
    success = _FakeHttpClient()
    client._http_client = lambda: success  # type: ignore[method-assign]
    result = client.create_pr(_request(), draft)
    assert result.ok
    put = next(call for call in success.calls if call[0] == "PUT")
    assert "Signed-off-by: Sentinel <sentinel@example.com>" in put[2]["json"]["message"]
    pull = next(call for call in success.calls if call[0] == "POST" and call[1].endswith("/pulls"))
    assert pull[2]["json"]["draft"] is True

    failed = _FakeHttpClient(fail_put=True)
    client._http_client = lambda: failed  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="HTTP 500"):
        client.create_pr(_request(), draft)
    assert any(call[0] == "DELETE" and "/git/refs/heads/" in call[1] for call in failed.calls)


class _FakeArgo(ArgoCdClient):
    def __init__(self) -> None:
        super().__init__(
            Settings(
                operational_targets={
                    "commerce": {"application": "commerce", "environment": "production"}
                }
            )
        )

    def _get_json(self, path: str) -> dict[str, Any]:
        if path == "/api/v1/applications":
            return {
                "items": [
                    {
                        "metadata": {"name": "commerce"},
                        "status": {
                            "sync": {"status": "OutOfSync"},
                            "health": {"status": "Degraded"},
                        },
                    },
                    {"metadata": {"name": "not-allowed"}, "status": {}},
                ]
            }
        return {
            "items": [
                {
                    "name": "commerce-api-1",
                    "namespace": "commerce",
                    "status": "Running",
                    "containers": ["api"],
                }
            ]
        }

    def _get_text(self, path: str, params: dict[str, str]) -> str:
        assert "/applications/commerce/pods/commerce-api-1/logs" in path
        assert params["namespace"] == "commerce"
        return "ready"


def test_argocd_listing_and_logs_are_allowlisted_and_rendered() -> None:
    argo = _FakeArgo()
    listed = argo.list_out_of_sync(_request(), {})
    assert "commerce" in listed.message
    assert "not-allowed" not in listed.message
    logs = argo.get_logs(
        _request(), {"_application": "commerce", "pod": "commerce-api-1", "tail_lines": 50}
    )
    assert "```\nready\n```" in logs.message
    with pytest.raises(RuntimeError, match="not managed"):
        argo.get_logs(_request(), {"_application": "commerce", "pod": "other"})


def test_inactive_access_user_is_denied_even_if_statically_admin(tmp_path: Path) -> None:
    users = tmp_path / "users.yaml"
    users.write_text(
        "users:\n  - id: former\n    slack: U-INACTIVE\n    role: admin\n    status: inactive\n",
        encoding="utf-8",
    )
    principal = IdentityResolver(
        Settings(access_users_path=str(users), admin_slack_user_ids={"U-INACTIVE"})
    ).resolve_slack_user("U-INACTIVE")
    assert principal.roles == set()


def test_deployment_pr_rejects_environment_mismatch() -> None:
    settings = Settings(
        gitops_targets={
            "commerce-api": {
                "path": "apps/commerce/api/deployment.yaml",
                "repository": "ghcr.io/example/commerce-api",
                "application": "commerce",
                "environment": "production",
            }
        }
    )
    registry = ToolRegistry(
        PolicyEngine(), GitHubClient(settings), AuditLogger(), _CapturingArgo(), GrafanaClient(settings)
    )
    result = registry.execute(
        _request(),
        "github_create_deploy_pr",
        {
            "service": "commerce-api",
            "environment": "staging",
            "image_tag": "sha256:" + "a" * 64,
        },
    )
    assert not result.ok
    assert "unsupported environment" in result.message


def test_access_tool_schemas_require_role() -> None:
    registry = ToolRegistry(
        PolicyEngine(), GitHubClient(Settings()), AuditLogger(), _CapturingArgo(), GrafanaClient(Settings())
    )
    schemas = {schema["name"]: schema for schema in registry.schemas}
    for name in ("github_create_grant_pr", "github_create_revoke_pr"):
        assert set(schemas[name]["parameters"]["required"]) == {"user", "role"}


def test_manifest_mutations_preserve_quotes_comments_and_existing_annotations() -> None:
    factory = GitOpsPullRequestFactory(Settings())
    old_digest = "a" * 64
    new_digest = "b" * 64
    target = {"path": "deployment.yaml", "repository": "ghcr.io/example/api"}
    manifest = f'          image: "ghcr.io/example/api@sha256:{old_digest}"  # api image\n'
    rendered = factory._render_image(
        manifest, target, f"ghcr.io/example/api@sha256:{new_digest}"
    )
    assert rendered == f'          image: "ghcr.io/example/api@sha256:{new_digest}"  # api image\n'

    deployment = """spec:
  template:
    metadata:
      labels:
        app: api
      annotations:
        prometheus.io/scrape: "true"
    spec: {}
"""
    restarted = factory._render_restart(deployment, "request-1")
    assert restarted.count("annotations:") == 1
    assert 'sentinel.dev/restartedAt: "request-1"' in restarted
    assert 'prometheus.io/scrape: "true"' in restarted


def test_access_pr_accepts_hujson_comments_and_trailing_commas() -> None:
    factory = GitOpsPullRequestFactory(_access_settings())
    draft = factory.access_change(
        _request(), {"action": "grant", "user": "alice@example.com", "role": "dev"}
    )
    policy = r"""{
      // managed roles
      "groups": {
        "group:gui-users": [],
        "group:dev": [],
        "group:operator": [],
        "group:admin": [],
      },
      "grants": [],
    }
    """
    rendered_text = draft.mutations[1].render(policy)
    assert "// managed roles" in rendered_text
    import hjson

    rendered = hjson.loads(rendered_text)
    assert rendered["groups"]["group:dev"] == ["alice@example.com"]


def test_access_sync_cli_does_not_require_tailscale_policy_for_argocd_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sentinel.access import sync as access_sync_module

    access = tmp_path / "access"
    access.mkdir()
    (access / "users.yaml").write_text("users: []\n", encoding="utf-8")
    (access / "roles.yaml").write_text("roles: {}\n", encoding="utf-8")
    output = tmp_path / "policy.csv"
    monkeypatch.setattr(
        "sys.argv",
        ["sentinel-access-sync", "--access-dir", str(access), "--argocd-policy-out", str(output)],
    )
    access_sync_module.main()
    assert output.read_text(encoding="utf-8") == "\n"

from __future__ import annotations

import base64
from typing import Any

import pytest

from sentinel.agent.orchestrator import AgentOrchestrator
from sentinel.agent.tools import ToolRegistry
from sentinel.audit import AuditLogger
from sentinel.config import Settings
from sentinel.integrations.argocd import ArgoCdClient
from sentinel.integrations.github import (
    FileMutation,
    GitHubClient,
    PreviousDigestNotFoundError,
    PullRequestDraft,
)
from sentinel.integrations.grafana import GrafanaClient
from sentinel.models import McpToolCall, OperationRequest, Principal, Role, ToolResult
from sentinel.policy import PolicyEngine


_REPOSITORY = "ghcr.io/example/commerce-api"
_PATH = "clusters/home/commerce/api.yaml"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "github_token": "test-token",
        "gitops_repo": "owner/repo",
        "github_default_branch": "main",
        "gitops_targets": {
            "commerce-api": {
                "path": _PATH,
                "repository": _REPOSITORY,
                "application": "commerce",
                "environment": "production",
            }
        },
    }
    values.update(overrides)
    return Settings(**values)


def _request() -> OperationRequest:
    return OperationRequest(
        request_id="request-previous",
        channel_id="C1",
        text="commerce-api 바로 이전 digest로 롤백해줘",
        principal=Principal("U1", "U1", None, {Role.GUI_USER}),
    )


def _manifest(digest_character: str) -> str:
    return (
        "apiVersion: apps/v1\nkind: Deployment\nspec:\n  template:\n"
        "    spec:\n      containers:\n        - name: api\n"
        f"          image: {_REPOSITORY}@sha256:{digest_character * 64}\n"
    )


class _Response:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _HistoryClient:
    def __init__(self, refs: dict[str, str]) -> None:
        self.refs = refs
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> "_HistoryClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append((url, kwargs))
        if "/git/ref/heads/" in url:
            return _Response({"object": {"sha": "base"}})
        if url.endswith("/commits"):
            return _Response([{"sha": ref} for ref in ("current", "same", "previous")])
        ref = str(kwargs.get("params", {}).get("ref") or "")
        content = self.refs.get(ref)
        if content is None:
            return _Response({}, 404)
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        return _Response({"content": encoded})


def test_previous_digest_is_latest_distinct_value_in_manifest_history() -> None:
    client = GitHubClient(_settings())
    history = _HistoryClient(
        {
            "base": _manifest("b"),
            "current": _manifest("b"),
            "same": _manifest("b"),
            "previous": _manifest("a"),
        }
    )
    client._http_client = lambda: history  # type: ignore[method-assign]

    current, previous, base_sha = client.previous_image_digests("commerce-api")

    assert current == "sha256:" + "b" * 64
    assert previous == "sha256:" + "a" * 64
    assert base_sha == "base"
    commits_call = next(call for call in history.calls if call[0].endswith("/commits"))
    assert commits_call[1]["params"] == {
        "path": _PATH,
        "sha": "base",
        "per_page": 100,
    }


def test_previous_digest_fails_closed_when_history_has_no_distinct_value() -> None:
    client = GitHubClient(_settings())
    history = _HistoryClient(
        {
            "base": _manifest("b"),
            "current": _manifest("b"),
            "same": _manifest("b"),
            "previous": _manifest("b"),
        }
    )
    client._http_client = lambda: history  # type: ignore[method-assign]

    with pytest.raises(PreviousDigestNotFoundError):
        client.previous_image_digests("commerce-api")


def test_previous_symbolic_target_creates_draft_with_resolved_digest() -> None:
    settings = _settings(github_pr_dry_run=True)
    github = GitHubClient(settings)
    github.previous_image_digests = lambda _service: (  # type: ignore[method-assign]
        "sha256:" + "b" * 64,
        "sha256:" + "a" * 64,
        "base",
    )
    registry = ToolRegistry(
        PolicyEngine(),
        github,
        AuditLogger(),
        ArgoCdClient(settings),
        GrafanaClient(settings),
    )

    result = registry.execute(
        _request(),
        "github_create_rollback_pr",
        {
            "service": "commerce-api",
            "environment": "production",
            "target": "previous",
        },
    )

    assert result.ok
    assert result.data["current_digest"] == "sha256:" + "b" * 64
    assert result.data["rollback_digest"] == "sha256:" + "a" * 64
    assert result.data["rollback_source"] == "previous_git_history"


class _RecordingMcp:
    def __init__(self) -> None:
        self.call: McpToolCall | None = None

    def list_tools(self) -> list[object]:
        return []

    def openai_tool_schemas(self) -> list[dict[str, object]]:
        return []

    def call_tool(self, _request: OperationRequest, call: McpToolCall) -> ToolResult:
        self.call = call
        return ToolResult(True, "ok")


def test_natural_language_previous_rollback_does_not_require_llm() -> None:
    mcp = _RecordingMcp()
    orchestrator = AgentOrchestrator(_settings(github_token=None), mcp)

    result = orchestrator.handle(_request())

    assert result.ok
    assert mcp.call is not None
    assert mcp.call.name == "github_create_rollback_pr"
    assert mcp.call.arguments == {
        "service": "commerce-api",
        "environment": "production",
        "target": "previous",
        "reason": "immediately previous digest requested",
    }


def test_previous_digest_skips_unusable_historical_manifests() -> None:
    client = GitHubClient(_settings())
    history = _HistoryClient(
        {
            "base": _manifest("b"),
            "current": _manifest("b"),
            "same": "image: ghcr.io/example/commerce-api:latest\n",
            "previous": _manifest("a"),
        }
    )
    client._http_client = lambda: history  # type: ignore[method-assign]

    current, previous, base_sha = client.previous_image_digests("commerce-api")

    assert current == "sha256:" + "b" * 64
    assert previous == "sha256:" + "a" * 64
    assert base_sha == "base"


class _AdvancedBaseClient:
    def __enter__(self) -> "_AdvancedBaseClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, url: str, **_kwargs: Any) -> _Response:
        if url.endswith("/pulls"):
            return _Response([])
        if "/git/ref/heads/" in url:
            return _Response({"object": {"sha": "advanced"}})
        raise AssertionError(f"unexpected GET {url}")


def test_rollback_pr_fails_when_default_branch_advanced() -> None:
    client = GitHubClient(
        _settings(
            github_commit_signoff="Tester <tester@example.com>",
            github_pr_dry_run=False,
        )
    )
    client._http_client = lambda: _AdvancedBaseClient()  # type: ignore[method-assign]
    draft = PullRequestDraft(
        action="rollback",
        title="revert: roll back commerce-api image",
        body="test",
        mutations=[FileMutation(_PATH, lambda current: str(current))],
        base_sha="base",
    )

    with pytest.raises(RuntimeError, match="default branch advanced"):
        client.create_pr(_request(), draft)

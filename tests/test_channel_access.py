from typing import Any

from sentinel.config import Settings
from sentinel.integrations.github import GitHubClient, PullRequestDraft
from sentinel.models import OperationRequest, Principal, ToolResult
from sentinel.policy import PolicyEngine
from sentinel.runtime import SentinelRuntime


def _request(channel: str = "C-ONBOARD") -> OperationRequest:
    return OperationRequest(
        request_id="request-1",
        channel_id=channel,
        text="status",
        principal=Principal("U-NEW", "U-NEW", None, set()),
    )


def test_unregistered_actor_is_read_only_in_configured_channel() -> None:
    policy = PolicyEngine({"C-ONBOARD"})
    request = _request()

    assert policy.authorize_request(request).allowed
    assert policy.authorize_tool_call(
        request, "argocd_get_status", {"environment": "production"}
    ).allowed
    assert policy.authorize_tool_call(
        request, "db_query_readonly", {"database": "commerce", "sql": "SELECT 1"}
    ).allowed
    assert not policy.authorize_tool_call(
        request,
        "github_create_deploy_pr",
        {"service": "commerce", "environment": "production"},
    ).allowed
    assert not policy.authorize_tool_call(
        request,
        "github_create_onboard_pr",
        {"user": "someone@example.com", "action": "onboard"},
    ).allowed


def test_unregistered_actor_remains_denied_outside_channel() -> None:
    policy = PolicyEngine({"C-ONBOARD"})
    request = _request("C-OTHER")
    assert not policy.authorize_request(request).allowed
    assert not policy.authorize_tool_call(
        request, "db_get_schema", {"database": "commerce"}
    ).allowed


def _runtime(users: str) -> tuple[SentinelRuntime, list[PullRequestDraft]]:
    runtime = SentinelRuntime(
        Settings(
            slack_onboarding_channel_id="C-ONBOARD",
        )
    )
    drafts: list[PullRequestDraft] = []
    runtime.github.read_file = lambda _path: users  # type: ignore[method-assign]

    def create_pr(_request: Any, draft: PullRequestDraft) -> ToolResult:
        drafts.append(draft)
        return ToolResult(True, "created", {"pull_request_url": "https://example.test/pr/1"})

    runtime.github.create_pr = create_pr  # type: ignore[method-assign]
    return runtime, drafts


def test_onboarding_creates_gui_user_draft_with_deterministic_key() -> None:
    runtime, drafts = _runtime("users: []\n")

    result = runtime.handle_onboarding("U-NEW", "C-ONBOARD", "Tailscale.User@example.com")

    assert result.ok
    assert result.data["onboarding_status"] == "created"
    assert result.data["tailscale_email"] == "ta***@example.com"
    assert len(drafts) == 1
    assert drafts[0].idempotency_key
    rendered = drafts[0].mutations[0].render("users: []\n")
    assert "role: gui-user" in rendered
    assert "slack_user_id: U-NEW" in rendered
    assert "email: tailscale.user@example.com" in rendered


def test_existing_slack_mapping_does_not_create_duplicate_pr() -> None:
    runtime, drafts = _runtime(
        """users:
  - id: known
    email: known@example.com
    slack: U-KNOWN
    role: gui-user
    status: active
"""
    )

    result = runtime.handle_onboarding("U-KNOWN", "C-ONBOARD", "known@example.com")

    assert result.ok
    assert result.data["onboarding_status"] == "already_registered"
    assert drafts == []


def test_canonical_slack_identity_is_matched_and_conflicts_are_rejected() -> None:
    users = """users:
  - id: known
    email: known@example.com
    slack_user_id: U-KNOWN
    role: gui-user
    status: active
"""
    runtime, drafts = _runtime(users)

    registered = runtime.handle_onboarding("U-KNOWN", "C-ONBOARD", "known@example.com")
    conflict = runtime.handle_onboarding("U-OTHER", "C-ONBOARD", "known@example.com")

    assert registered.ok
    assert registered.data["onboarding_status"] == "already_registered"
    assert not conflict.ok
    assert drafts == []


def test_onboarding_rejects_slack_or_tailscale_account_conflict() -> None:
    users = """users:
  - id: known
    email: known@example.com
    slack: U-KNOWN
    role: gui-user
    status: active
"""
    runtime, drafts = _runtime(users)

    wrong_email = runtime.handle_onboarding("U-KNOWN", "C-ONBOARD", "other@example.com")
    wrong_slack = runtime.handle_onboarding("U-OTHER", "C-ONBOARD", "known@example.com")

    assert not wrong_email.ok
    assert not wrong_slack.ok
    assert drafts == []


def test_onboarding_is_rejected_outside_configured_channel() -> None:
    runtime, drafts = _runtime("users: []\n")
    result = runtime.handle_onboarding("U-NEW", "C-OTHER", "new@example.com")
    assert not result.ok
    assert drafts == []


class _Response:
    status_code = 200

    def json(self) -> list[dict[str, str]]:
        return [{"html_url": "https://example.test/pr/7"}]

    def raise_for_status(self) -> None:
        return None


class _ExistingPrClient:
    def __init__(self) -> None:
        self.get_calls: list[tuple[str, dict[str, Any]]] = []
        self.write_called = False

    def __enter__(self) -> "_ExistingPrClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.get_calls.append((url, kwargs))
        return _Response()

    def post(self, *_args: Any, **_kwargs: Any) -> None:
        self.write_called = True


def test_existing_deterministic_onboarding_pr_is_reused() -> None:
    settings = Settings(
        github_pr_dry_run=False,
        github_token="synthetic-test-value",
        github_commit_signoff="Sentinel Test <sentinel@example.invalid>",
        gitops_repo="owner/repo",
    )
    github = GitHubClient(settings)
    http = _ExistingPrClient()
    github._http_client = lambda: http  # type: ignore[method-assign]
    draft = PullRequestDraft("access-onboard", "access: onboard", "body", [], "abc123")

    result = github.create_pr(_request(), draft)

    assert result.ok
    assert result.data["already_pending"] is True
    assert result.data["pull_request_url"] == "https://example.test/pr/7"
    assert http.get_calls[0][1]["params"]["head"] == "owner:fix/sentinel-access-onboard-abc123"
    assert not http.write_called

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def _env_set(name: str) -> set[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return set()
    if raw.startswith("["):
        loaded = json.loads(raw)
        return {str(item) for item in loaded}
    return {item.strip() for item in raw.split(",") if item.strip()}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_object(name: str) -> dict[str, dict[str, str]]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return {
        str(key): {str(field): str(value) for field, value in item.items()}
        for key, item in loaded.items()
        if isinstance(item, dict)
    }


@dataclass(frozen=True)
class Settings:
    env: str = field(default_factory=lambda: os.getenv("SENTINEL_ENV", "development"))
    log_level: str = field(default_factory=lambda: os.getenv("SENTINEL_LOG_LEVEL", "INFO"))

    openai_api_key: str | None = field(
        default_factory=lambda: os.getenv("SENTINEL_OPENAI_API_KEY") or None
    )
    openai_model: str = field(
        default_factory=lambda: os.getenv("SENTINEL_OPENAI_MODEL", "gpt-4.1-mini")
    )

    gemini_api_key: str | None = field(
        default_factory=lambda: os.getenv("SENTINEL_GEMINI_API_KEY") or None
    )
    gemini_model: str = field(
        default_factory=lambda: os.getenv("SENTINEL_GEMINI_MODEL", "gemini-3.5-flash")
    )
    gemini_base_url: str = field(
        default_factory=lambda: os.getenv(
            "SENTINEL_GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
        )
    )

    slack_bot_token: str | None = field(
        default_factory=lambda: os.getenv("SENTINEL_SLACK_BOT_TOKEN") or None
    )
    slack_app_token: str | None = field(
        default_factory=lambda: os.getenv("SENTINEL_SLACK_APP_TOKEN") or None
    )
    slack_signing_secret: str | None = field(
        default_factory=lambda: os.getenv("SENTINEL_SLACK_SIGNING_SECRET") or None
    )
    slack_control_channels: set[str] = field(
        default_factory=lambda: _env_set("SENTINEL_SLACK_CONTROL_CHANNELS")
    )
    slack_alert_channel_id: str | None = field(
        default_factory=lambda: os.getenv("SENTINEL_SLACK_ALERT_CHANNEL_ID") or None
    )
    slack_allow_dms: bool = field(
        default_factory=lambda: _env_bool("SENTINEL_SLACK_ALLOW_DMS", False)
    )
    slack_onboarding_channel_id: str | None = field(
        default_factory=lambda: os.getenv("SENTINEL_SLACK_ONBOARDING_CHANNEL_ID") or None
    )
    slack_welcome_thread_ts: str | None = field(
        default_factory=lambda: os.getenv("SENTINEL_SLACK_WELCOME_THREAD_TS") or None
    )

    gitops_repo: str = field(
        default_factory=lambda: os.getenv("SENTINEL_GITOPS_REPO", "example/cluster-config")
    )
    github_token: str | None = field(
        default_factory=lambda: os.getenv("SENTINEL_GITHUB_TOKEN") or None
    )
    github_default_branch: str = field(
        default_factory=lambda: os.getenv("SENTINEL_GITHUB_DEFAULT_BRANCH", "main")
    )
    github_pr_dry_run: bool = field(
        default_factory=lambda: _env_bool("SENTINEL_GITHUB_PR_DRY_RUN", True)
    )
    github_commit_signoff: str | None = field(
        default_factory=lambda: os.getenv("SENTINEL_GITHUB_COMMIT_SIGNOFF") or None
    )
    gitops_targets: dict[str, dict[str, str]] = field(
        default_factory=lambda: _env_object("SENTINEL_GITOPS_TARGETS")
    )
    operational_targets: dict[str, dict[str, str]] = field(
        default_factory=lambda: _env_object("SENTINEL_OPERATIONAL_TARGETS")
    )
    access_role_groups: dict[str, dict[str, str]] = field(
        default_factory=lambda: _env_object("SENTINEL_ACCESS_ROLE_GROUPS")
    )

    argocd_base_url: str | None = field(
        default_factory=lambda: os.getenv("SENTINEL_ARGOCD_BASE_URL") or None
    )
    argocd_token: str | None = field(
        default_factory=lambda: os.getenv("SENTINEL_ARGOCD_TOKEN") or None
    )
    argocd_app_name_template: str = field(
        default_factory=lambda: os.getenv(
            "SENTINEL_ARGOCD_APP_NAME_TEMPLATE", "{service}-{environment}"
        )
    )

    grafana_base_url: str | None = field(
        default_factory=lambda: os.getenv("SENTINEL_GRAFANA_BASE_URL") or None
    )
    grafana_token: str | None = field(
        default_factory=lambda: os.getenv("SENTINEL_GRAFANA_TOKEN") or None
    )

    access_users_path: str | None = field(
        default_factory=lambda: os.getenv("SENTINEL_ACCESS_USERS_PATH") or None
    )
    admin_slack_user_ids: set[str] = field(
        default_factory=lambda: _env_set("SENTINEL_ADMIN_SLACK_USER_IDS")
    )
    operator_slack_user_ids: set[str] = field(
        default_factory=lambda: _env_set("SENTINEL_OPERATOR_SLACK_USER_IDS")
    )
    db_read_enabled: bool = field(
        default_factory=lambda: _env_bool("SENTINEL_DB_READ_ENABLED", False)
    )
    db_read_targets: dict[str, dict[str, str]] = field(
        default_factory=lambda: _env_object("SENTINEL_DB_READ_TARGETS")
    )

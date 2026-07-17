from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sentinel.config import Settings
from sentinel.models import Principal, Role


class IdentityResolver:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._users = self._load_users(settings.access_users_path)

    def resolve_slack_user(self, slack_user_id: str) -> Principal:
        user = self._users.get(slack_user_id)
        if user and str(user.get("status", "active")) != "active":
            return Principal(slack_user_id, slack_user_id, None, set(), set())
        if user:
            raw_roles = user.get("roles")
            access_roles = raw_roles if isinstance(raw_roles, list) else [user.get("role", "dev")]
            return Principal(
                user_id=str(user.get("id") or slack_user_id),
                slack_user_id=slack_user_id,
                github_username=str(user.get("github_username") or user.get("github"))
                if user.get("github_username") or user.get("github")
                else None,
                roles={Role(str(role)) for role in access_roles},
                groups={str(group) for group in user.get("groups", [])},
            )

        if slack_user_id in self.settings.admin_slack_user_ids:
            roles = {Role.ADMIN}
        elif slack_user_id in self.settings.operator_slack_user_ids:
            roles = {Role.OPERATOR}
        else:
            roles = set()

        return Principal(
            user_id=slack_user_id,
            slack_user_id=slack_user_id,
            github_username=None,
            roles=roles,
            groups=set(),
        )

    def _load_users(self, path: str | None) -> dict[str, dict[str, Any]]:
        if not path:
            return {}
        text = Path(path).read_text(encoding="utf-8")
        if path.endswith(".json"):
            raw = json.loads(text)
        else:
            try:
                import yaml
            except ImportError as exc:  # pragma: no cover - optional dependency boundary
                raise RuntimeError("PyYAML is required for YAML access files") from exc
            raw = yaml.safe_load(text) or {}
        if not isinstance(raw, dict):
            return {}
        users = raw.get("users", [])
        if not isinstance(users, list):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for user in users:
            if isinstance(user, dict):
                slack_user_id = user.get("slack_user_id") or user.get("slack")
                if slack_user_id:
                    result[str(slack_user_id)] = dict(user)
        return result

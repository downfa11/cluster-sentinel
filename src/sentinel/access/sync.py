from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class AccessUser:
    email: str
    slack_user_id: str | None = None
    github_username: str | None = None
    roles: set[str] = field(default_factory=set)
    groups: set[str] = field(default_factory=set)
    status: str = "active"


@dataclass(frozen=True)
class AccessGroup:
    name: str
    github_team: str | None = None
    grafana_team: str | None = None
    tailscale_group: str | None = None
    services: set[str] = field(default_factory=set)


class AccessDirectory:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.users = self._load_users()
        self.groups = self._load_role_groups()

    def active_users(self) -> list[AccessUser]:
        return [user for user in self.users if user.status == "active"]

    def _load_users(self) -> list[AccessUser]:
        raw = self._load_yaml("users.yaml").get("users", [])
        users: list[AccessUser] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            raw_roles = item.get("roles")
            roles = raw_roles if isinstance(raw_roles, list) else [item.get("role")]
            groups = {str(group) for group in item.get("groups", [])}
            groups.update(str(role) for role in roles if role)
            users.append(
                AccessUser(
                    email=str(item.get("email") or item.get("id") or ""),
                    slack_user_id=item.get("slack_user_id") or item.get("slack"),
                    github_username=item.get("github_username") or item.get("github"),
                    roles={str(role) for role in roles if role},
                    groups=groups,
                    status=str(item.get("status", "active")),
                )
            )
        return users

    def _load_role_groups(self) -> dict[str, AccessGroup]:
        raw = self._load_yaml("roles.yaml").get("roles", {})
        groups: dict[str, AccessGroup] = {}
        for name, item in raw.items():
            if not isinstance(item, dict):
                continue
            groups[str(name)] = AccessGroup(
                name=str(name),
                github_team=item.get("github_team"),
                grafana_team=item.get("grafana_team"),
                tailscale_group=item.get("tailscale_group"),
                services={str(service) for service in item.get("services", [])},
            )
        return groups

    def _load_yaml(self, filename: str) -> dict[str, Any]:
        path = self.root / filename
        if not path.exists():
            return {}
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise RuntimeError(f"{path} must contain a YAML mapping")
        return loaded


class AccessSync:
    def __init__(self, access: AccessDirectory, dry_run: bool, sync_removals: bool = False) -> None:
        self.access = access
        self.dry_run = dry_run
        self.sync_removals = sync_removals

    def render_tailscale_policy(
        self, existing_policy: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if existing_policy is None:
            raise RuntimeError("an existing Tailscale policy is required")
        managed_groups = self.render_tailscale_groups()
        if not managed_groups:
            raise RuntimeError("access/roles.yaml defines no managed Tailscale groups")
        policy = dict(existing_policy)
        current_groups = policy.get("groups")
        if not isinstance(current_groups, dict):
            raise RuntimeError("the existing Tailscale policy must contain a groups mapping")
        policy["groups"] = {**current_groups, **managed_groups}
        return policy

    def render_tailscale_groups(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for group_name, group in self.access.groups.items():
            if not group.tailscale_group:
                continue
            members = [
                user.email for user in self.access.active_users() if group_name in user.groups
            ]
            groups[group.tailscale_group] = sorted(members)
        return groups

    def render_argocd_policy_csv(self) -> str:
        lines = []
        for user in self.access.active_users():
            for role in sorted(user.roles):
                lines.append(f"g, {user.email}, role:{role}")
            for group_name in sorted(user.groups):
                lines.append(f"g, {user.email}, role:{group_name}")
        for group_name, group in self.access.groups.items():
            for service in sorted(group.services):
                lines.append(f"p, role:{group_name}, applications, get, */{service}-*, allow")
                lines.append(f"p, role:{group_name}, logs, get, */{service}-*, allow")
        return "\n".join(sorted(set(lines))) + "\n"

    def sync_github_teams(self, org: str, token: str | None) -> list[str]:
        operations: list[str] = []
        desired_by_team = self._github_desired_members()
        for slug, desired in desired_by_team.items():
            operations.append(f"github:{org}/{slug} desired={','.join(sorted(desired))}")
        if self.dry_run or not token:
            return operations

        import httpx

        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        with httpx.Client(timeout=30.0, headers=headers) as client:
            for slug, desired in desired_by_team.items():
                current = self._github_current_members(client, org, slug)
                for username in sorted(desired - current):
                    response = client.put(
                        f"https://api.github.com/orgs/{org}/teams/{slug}/memberships/{username}",
                        json={"role": "member"},
                    )
                    response.raise_for_status()
                if not self.sync_removals:
                    continue
                for username in sorted(current - desired):
                    response = client.delete(
                        f"https://api.github.com/orgs/{org}/teams/{slug}/memberships/{username}"
                    )
                    if response.status_code != 404:
                        response.raise_for_status()
        return operations

    def sync_grafana_teams(self, base_url: str | None, token: str | None) -> list[str]:
        operations: list[str] = []
        desired_by_team = self._grafana_desired_members()
        for team_name, desired in desired_by_team.items():
            operations.append(f"grafana:{team_name} desired={','.join(sorted(desired))}")
        if self.dry_run or not base_url or not token:
            return operations

        import httpx

        root = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(timeout=30.0, headers=headers) as client:
            for team_name, desired in desired_by_team.items():
                team_id = self._grafana_ensure_team(client, root, team_name)
                current = self._grafana_current_members(client, root, team_id)
                current_emails = {member["email"] for member in current}
                for email in sorted(desired - current_emails):
                    response = client.post(
                        f"{root}/api/teams/{team_id}/members",
                        json={"loginOrEmail": email},
                    )
                    if response.status_code not in {200, 409}:
                        response.raise_for_status()
                if not self.sync_removals:
                    continue
                for member in current:
                    if member["email"] in desired:
                        continue
                    response = client.delete(
                        f"{root}/api/teams/{team_id}/members/{member['userId']}"
                    )
                    if response.status_code != 404:
                        response.raise_for_status()
        return operations

    def publish_tailscale_policy(
        self,
        tailnet: str | None,
        token: str | None,
        policy: dict[str, Any],
    ) -> list[str]:
        if not tailnet:
            return []
        operations = [f"tailscale:{tailnet} groups={len(policy.get('groups', {}))}"]
        if self.dry_run or not token:
            return operations

        import httpx

        response = httpx.post(
            f"https://api.tailscale.com/api/v2/tailnet/{tailnet}/acl",
            headers={"Authorization": f"Bearer {token}"},
            json=policy,
            timeout=30.0,
        )
        response.raise_for_status()
        return operations

    def _github_desired_members(self) -> dict[str, set[str]]:
        desired: dict[str, set[str]] = {}
        for group_name, group in self.access.groups.items():
            if not group.github_team:
                continue
            members = {
                user.github_username
                for user in self.access.active_users()
                if group_name in user.groups and user.github_username
            }
            desired[group.github_team] = {str(member) for member in members if member}
        return desired

    def _grafana_desired_members(self) -> dict[str, set[str]]:
        desired: dict[str, set[str]] = {}
        for group_name, group in self.access.groups.items():
            if not group.grafana_team:
                continue
            members = {
                user.email for user in self.access.active_users() if group_name in user.groups
            }
            desired[group.grafana_team] = members
        return desired

    def _github_current_members(self, client: Any, org: str, slug: str) -> set[str]:
        members: set[str] = set()
        page = 1
        while True:
            response = client.get(
                f"https://api.github.com/orgs/{org}/teams/{slug}/members",
                params={"per_page": 100, "page": page},
            )
            response.raise_for_status()
            payload = response.json()
            if not payload:
                return members
            members.update(str(item["login"]) for item in payload if item.get("login"))
            page += 1

    def _grafana_ensure_team(self, client: Any, root: str, team_name: str) -> int:
        teams_response = client.get(f"{root}/api/teams/search", params={"name": team_name})
        teams_response.raise_for_status()
        teams = teams_response.json().get("teams", [])
        for team in teams:
            if team.get("name") == team_name:
                return int(team["id"])
        created = client.post(f"{root}/api/teams", json={"name": team_name})
        created.raise_for_status()
        payload = created.json()
        return int(payload.get("teamId") or payload.get("id"))

    def _grafana_current_members(
        self, client: Any, root: str, team_id: int
    ) -> list[dict[str, Any]]:
        response = client.get(f"{root}/api/teams/{team_id}/members")
        response.raise_for_status()
        members = []
        for item in response.json():
            email = item.get("email") or item.get("login")
            user_id = item.get("userId")
            if email and user_id:
                members.append({"email": str(email), "userId": int(user_id)})
        return members


def _load_policy(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    policy_path = Path(path)
    if not policy_path.exists():
        raise RuntimeError(f"Tailscale policy does not exist: {policy_path}")
    loaded = json.loads(policy_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError(f"{policy_path} must contain a JSON object")
    return loaded


def _write_json(path: str, payload: dict[str, Any]) -> str:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(output)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Sentinel access source of truth")
    parser.add_argument("--access-dir", default="access")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--sync-removals", action="store_true")
    parser.add_argument("--github-org")
    parser.add_argument("--github-token")
    parser.add_argument("--grafana-url")
    parser.add_argument("--grafana-token")
    parser.add_argument("--tailscale-tailnet")
    parser.add_argument("--tailscale-token")
    parser.add_argument("--tailscale-policy-in")
    parser.add_argument("--tailscale-policy-out")
    parser.add_argument("--tailscale-groups-out")
    parser.add_argument("--argocd-policy-out")
    args = parser.parse_args()

    sync = AccessSync(
        AccessDirectory(Path(args.access_dir)),
        dry_run=args.dry_run,
        sync_removals=args.sync_removals,
    )
    tailscale_policy = sync.render_tailscale_policy(_load_policy(args.tailscale_policy_in))
    result: dict[str, Any] = {
        "github": sync.sync_github_teams(args.github_org or "", args.github_token)
        if args.github_org
        else [],
        "grafana": sync.sync_grafana_teams(args.grafana_url, args.grafana_token),
        "tailscale": sync.publish_tailscale_policy(
            args.tailscale_tailnet,
            args.tailscale_token,
            tailscale_policy,
        ),
    }

    if args.tailscale_groups_out:
        result["tailscale_groups_out"] = _write_json(
            args.tailscale_groups_out,
            {"groups": sync.render_tailscale_groups()},
        )

    if args.tailscale_policy_out:
        result["tailscale_policy_out"] = _write_json(args.tailscale_policy_out, tailscale_policy)

    if args.argocd_policy_out:
        path = Path(args.argocd_policy_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sync.render_argocd_policy_csv(), encoding="utf-8")
        result["argocd_policy_out"] = str(path)

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

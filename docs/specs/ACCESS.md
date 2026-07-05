# Access Model

## 1. Source of truth

Access is managed in the private GitOps repository:

```text
access/users.yaml
access/roles.yaml
access/groups.yaml
```

Onboarding, offboarding, grants, and revocations begin as PRs against these files. GitHub Actions sync approved changes to GitHub teams, Tailscale ACLs, Grafana teams, and Argo CD RBAC.

## 2. Roles

| Role | Slack commands | GitHub | Argo CD | Grafana | Tailscale |
| --- | --- | --- | --- | --- | --- |
| dev | status, logs, deploy non-prod owned service, whois, members | read, PR author for owned non-prod | read owned apps | view owned dashboards | app dashboards for owned services |
| operator | status, logs, deploy, restart, rollback, whois, members | PR author, reviewer for ops changes | read all apps, sync non-prod by workflow only | view ops dashboards | ops UIs |
| admin | all commands, access management | repository admin or app admin | admin through approved channels | admin | admin ACL groups |
| bot | internal automation only | GitHub App scoped writes | API reads and workflow-scoped actions | API reads | service identity |

## 3. users.yaml

```yaml
users:
  - id: user-001
    name: Example User
    email: user@example.com
    slack_user_id: U00000000
    github_username: example-user
    roles:
      - dev
    groups:
      - api-team
    status: active
    expires_at: null
```

## 4. roles.yaml

```yaml
roles:
  dev:
    commands: [status, logs, deploy, whois, members]
    environments: [dev, staging]
    production: false
  operator:
    commands: [status, logs, deploy, restart, rollback, whois, members]
    environments: [dev, staging, production]
    production_requires_approval: true
  admin:
    commands: ["*"]
    environments: ["*"]
```

## 5. groups.yaml

```yaml
groups:
  api-team:
    owners: [admin@example.com]
    services: [api]
    environments: [dev, staging]
    github_team: api-team
    grafana_team: api-team
    tailscale_group: group:api-team
```

## 6. Synchronization targets

- GitHub teams and repository permissions.
- Tailscale ACL groups and tags.
- Grafana teams and folder permissions.
- Argo CD RBAC policy entries.

## 7. Expiration

Temporary roles must include `expires_at`. The access workflow opens automatic revocation PRs before expiration or fails policy checks after expiration.

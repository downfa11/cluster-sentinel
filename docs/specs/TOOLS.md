# Tool Specification

Sentinel exposes MCP-style tools to the LLM. The model selects tools, but Sentinel executes them only after policy authorization.

## Safety rule

No tool may execute shell commands, run kubectl, run terraform, SSH into hosts, read secrets, or mutate Kubernetes directly. Write tools create GitHub pull requests only.

## Tools

### github_create_deploy_pr

Creates a GitOps PR that patches Helm values at `apps/{service}/overlays/{environment}/values.yaml` by updating:

```yaml
image:
  repository: ghcr.io/example/api
  tag: v1
```

Required inputs: `service`, `environment`, `image_tag`.

### github_create_restart_pr

Creates a GitOps PR that patches:

```yaml
podAnnotations:
  sentinel.dev/restartedAt: <request-id>
```

Required inputs: `service`, `environment`.

### github_create_rollback_pr

Creates a GitOps PR that patches Helm image values to the rollback target.

Required inputs: `service`, `environment`, `target`.

### github_create_onboard_pr / github_create_offboard_pr / github_create_grant_pr / github_create_revoke_pr

Creates reviewable PRs that patch `access/users.yaml`. After merge, `sentinel-access-sync` reconciles the approved state to GitHub teams, Grafana teams, Tailscale policy, and Argo CD RBAC policy output.

### argocd_get_status

Calls Argo CD API `GET /api/v1/applications/{app}` and returns health, sync status, revision, and conditions.

### argocd_diff

Calls Argo CD managed resources API and returns a redacted resource summary.

### grafana_alerts

Calls Grafana alert APIs and returns matching alert data.

### access_get_user

Returns non-sensitive access metadata. Current implementation is a placeholder until access source-of-truth lookup is expanded.

## Removed

Loki/log query tooling is intentionally not included in this implementation.

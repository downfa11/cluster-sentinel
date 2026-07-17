# Sentinel Documentation

## Concept

Sentinel is not a command bot. It is an LLM-driven GitOps agent. Slack messages are natural language inputs; the model selects MCP-style tools; the policy engine authorizes the exact tool call; write tools create GitHub pull requests.

## Implemented Tools

- `github_create_deploy_pr`: patches Helm values image fields and opens a PR.
- `github_create_restart_pr`: patches restart annotation and opens a PR.
- `github_create_rollback_pr`: patches image fields to the rollback target and opens a PR.
- `github_create_onboard_pr`, `github_create_offboard_pr`, `github_create_grant_pr`, `github_create_revoke_pr`: patch `access/users.yaml` in a reviewable PR.
- `argocd_get_status`: calls Argo CD API.
- `argocd_diff`: reads Argo CD managed resources.
- `grafana_alerts`: calls Grafana alert APIs.
- `access_get_user`: local access metadata placeholder.

Loki is intentionally not included.

## Required Runtime

Sentinel requires `SENTINEL_GEMINI_API_KEY` or `SENTINEL_OPENAI_API_KEY`; Gemini is preferred when both exist. Without either key, Sentinel refuses to guess tools.

## GitOps Writes

The default values path is `apps/{service}/overlays/{environment}/values.yaml`. Deploy and rollback update:

```yaml
image:
  repository: ghcr.io/example/api
  tag: v1
```

Restart updates:

```yaml
podAnnotations:
  sentinel.dev/restartedAt: <request-id>
```

## Access Sync

Merged access changes are applied by `sentinel-access-sync`. It reconciles GitHub teams and Grafana teams through their APIs, renders Tailscale policy JSON, can publish that policy through the Tailscale API, and generates Argo CD RBAC CSV for GitOps-managed Argo CD configuration.
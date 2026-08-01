# Tool Specification

The LLM may select only these tools. Sentinel resolves applications and environments from server-side allowlists before policy authorization.

## GitOps PR tools

- `github_create_deploy_pr(service, environment, image_tag)`: replace the configured image with an immutable sha256 digest.
- `github_create_restart_pr(service, environment)`: update the configured Deployment Pod-template restart annotation.
- `github_create_rollback_pr(service, environment, target)`: replace the configured image with an immutable rollback digest. The symbolic target `previous` resolves server-side to the most recent distinct digest in up to 100 commits for the allowlisted manifest path.
- `github_create_onboard_pr`, `github_create_offboard_pr`, `github_create_grant_pr`, `github_create_revoke_pr`: update `access/users.yaml` and the managed role groups in `external/tailscale/policy.hujson`.

Every registered human Sentinel role may create these signed-off draft PRs. The policy records admin review as required, and Sentinel never merges a PR. Unregistered channel users and bot identities remain read-only.

## Argo CD read tools

- `argocd_list_applications()`: list allowlisted applications with health and sync state.
- `argocd_list_out_of_sync()`: list allowlisted OutOfSync applications.
- `argocd_get_status(service)`: read health, sync, revision, and conditions.
- `argocd_diff(service)`: list managed resources.
- `argocd_list_pods(service)`: list Pods managed by the application.
- `argocd_get_logs(service, pod?, container?, tail_lines?)`: read 1–500 recent lines from a Pod and container that belong to the allowlisted application. Output is capped before returning to Slack.

## Other read tools

- `grafana_alerts(service)`: return active matching alert summaries.
- `access_get_user(user)`: read non-sensitive metadata from the GitHub-managed access source.

No tool runs shell commands, kubectl, terraform, SSH, secret reads, direct sync, direct Kubernetes mutation, or PR merge.

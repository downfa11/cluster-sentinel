# Sentinel Documentation

Sentinel is a natural-language Slack GitOps agent. Gemini or OpenAI selects an allowlisted tool, the policy engine authorizes the server-resolved target, and write tools create draft GitHub pull requests. Sentinel never merges PRs or mutates Kubernetes directly.

## Implemented tools

- Digest-pinned deploy, rollback, and restart draft PRs for explicitly configured workloads.
- Access onboarding, offboarding, grant, and revoke draft PRs that update both `access/users.yaml` and `external/tailscale/policy.hujson`.
- Argo CD application list, OutOfSync list, health/sync status, managed resources, managed Pod list, and bounded recent Pod logs.
- Grafana active alert reads with visible Slack summaries.
- GitHub-backed non-sensitive access metadata lookup.

All Argo CD and Grafana service names resolve through `SENTINEL_OPERATIONAL_TARGETS`; caller-provided environment strings cannot select a different application. Unknown Slack users have no role and are denied.

## GitOps writes

`SENTINEL_GITOPS_TARGETS` defines each writable manifest path, image repository, Argo CD application, and environment. Deploy and rollback replace exactly one allowlisted `repository@sha256:...` image. Restart updates `sentinel.dev/restartedAt` in the Deployment Pod template. Live PR commits include DCO sign-off, PRs are draft, and failed creation removes the temporary branch.

## Access sync

The workflow belongs in `cluster-config`, where the source files live. It verifies that `access/users.yaml`, `access/roles.yaml`, and the reviewed Tailscale policy agree before publishing the policy. Unmanaged policy groups and all non-group policy fields are preserved.

Channel messages require a bot mention. DMs are disabled by default and can be enabled explicitly with `SENTINEL_SLACK_ALLOW_DMS=true`.

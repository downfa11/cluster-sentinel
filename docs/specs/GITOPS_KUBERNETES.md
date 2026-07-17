# GitOps and Kubernetes Specification

Sentinel writes only through draft GitHub pull requests. `SENTINEL_GITOPS_TARGETS` is the server-side allowlist for manifest path, immutable image repository, Argo CD application, and environment.

- Deploy and rollback replace exactly one configured `repository@sha256:...` image.
- Restart updates `sentinel.dev/restartedAt` under Deployment `spec.template.metadata`.
- Access changes update both `access/users.yaml` and the reviewed Tailscale policy.
- The cluster-config access workflow validates the rendered policy before publishing it.

Read operations use Argo CD and Grafana APIs. Argo CD Pod logs are limited to Pods returned for the allowlisted application; arbitrary namespace or Pod access is rejected.

The Sentinel service account does not require broad Kubernetes mutation rights. Sentinel does not expose direct Argo CD sync, PR merge, kubectl, SSH, arbitrary shell, or secret-read tools.

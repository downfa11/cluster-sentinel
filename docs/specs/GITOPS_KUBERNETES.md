# GitOps and Kubernetes Specification

## Private cluster-config structure

```text
cluster-config/
  access/
    users.yaml
    roles.yaml
    groups.yaml
  argocd/
  apps/
    api/
      overlays/dev/values.yaml
      overlays/staging/values.yaml
      overlays/production/values.yaml
  monitoring/
    prometheus/
    grafana/
  rbac/
  slack/
  tailscale/
```

## Sentinel writes

Sentinel writes only through GitHub pull requests. Deploy and rollback tools patch Helm values files. Restart tools patch restart annotations in the same values file.

## Namespaces

- `sentinel-system`
- `argocd`
- `monitoring`
- application namespaces such as `apps-dev`, `apps-staging`, `apps-production`

## Service account

The Sentinel Kubernetes service account should not have broad mutation rights. Runtime integrations should use GitHub, Argo CD read APIs, Grafana read APIs, and audit storage.

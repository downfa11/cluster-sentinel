# Future Expansion Plan

## 1. Additional interfaces

Sentinel should not be positioned as only a Slack bot. The product identity is an AI GitOps DevOps Agent. Future interfaces can reuse the same policy engine and tool layer:

- Web UI for operation history and access review.
- CLI for platform engineers.
- MCP server for approved read-only operational context.
- Chat clients beyond Slack.

## 2. More GitOps targets

- Flux support alongside Argo CD.
- Multi-cluster routing.
- Environment promotion pipelines.
- Progressive delivery with Argo Rollouts.

## 3. Deeper governance

- Access recertification campaigns.
- Temporary privileged access with automatic revocation PRs.
- Policy-as-code using OPA or Cedar if local rules outgrow Python.
- Signed policy bundles.

## 4. Incident workflows

- Incident ID integration.
- Break-glass approval path.
- Post-incident audit export.
- Automated rollback recommendation without direct execution.

## 5. Marketplace readiness

- Public demo cluster with fake services.
- GIF showing Slack request to PR to Argo CD sync.
- Example GitOps repository template.
- Quickstart using k3d or local k3s.

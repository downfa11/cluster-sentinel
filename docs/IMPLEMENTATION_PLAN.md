# Implementation Plan

## Implemented

- Slack Socket Mode app mention handling and optional DM handling
- Gemini/OpenAI tool calling with no heuristic fallback
- fail-closed Slack identity and per-tool authorization
- server-side GitOps and operational target allowlists
- digest-pinned deploy/rollback and annotation restart draft PRs
- access user plus reviewed Tailscale policy draft PRs
- Argo CD app/status/resource/Pod/log reads and Grafana alert reads
- JSON audit logs and visible multi-tool Slack results
- DCO draft PR creation with orphan-branch cleanup tests

## Future hardening

- GitHub App authentication instead of a broad token
- structured Slack approval UX
- audit storage with retention and querying
- access expiration and periodic reconciliation reporting

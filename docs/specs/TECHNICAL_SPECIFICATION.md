# Technical Specification

## Runtime

- Language: Python 3.12+
- AI: OpenAI Responses API tool calling
- Slack: Slack Bolt for Python, Socket Mode
- GitOps writes: GitHub Pull Request only
- Cluster read state: Argo CD API
- Alerts: Grafana API
- Registry: GHCR
- CI/CD: GitHub Actions

## Request flow

```text
Slack natural language
-> Sentinel runtime
-> OpenAI Responses API
-> MCP-style tool selection
-> Policy Engine authorization
-> GitHub PR or read-only API call
-> audit log
-> Slack response
```

## Required environment

`SENTINEL_OPENAI_API_KEY` is required. Sentinel refuses to guess tool calls without the LLM.

## Forbidden capabilities

- `kubectl apply`
- `kubectl delete`
- `terraform apply`
- SSH
- arbitrary shell execution
- secret reads
- direct Kubernetes mutation

## Package layout

```text
src/sentinel/
  app.py
  runtime.py
  config.py
  policy.py
  agent/
    orchestrator.py
    mcp.py
    tools.py
  slack/
    app.py
  integrations/
    github.py
    argocd.py
    grafana.py
  audit.py
  identity.py
```

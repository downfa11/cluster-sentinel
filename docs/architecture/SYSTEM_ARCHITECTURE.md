# System Architecture

Sentinel is an AI GitOps DevOps Agent for Slack.

```text
Slack DM / mention
  -> Sentinel Slack app
  -> OpenAI Responses API
  -> MCP-style tool gateway
  -> Policy Engine
  -> GitHub PR / Argo CD API / Grafana API
  -> Audit log
```

## Trust boundaries

- The LLM does not get shell, kubectl, terraform, SSH, or secret-read tools.
- Write tools create GitHub pull requests only.
- Argo CD and Grafana tools are read-only API clients.
- Policy authorization happens after tool selection and before execution.

## Implemented integrations

- GitHub Contents/Pulls API for PR creation
- Argo CD API for app status and managed resources
- Grafana API for alert reads
- Slack Socket Mode
- OpenAI Responses API

Loki is not part of this implementation.

# Security Design

## Least privilege

- GitHub token or GitHub App permissions should be scoped to the GitOps repository.
- Argo CD token should be read-only.
- Grafana token should be read-only for alert APIs.
- Slack scopes should be minimal.
- OpenAI prompts must not include secrets.

## Branch protection

The private GitOps repository should require pull request reviews, required checks, and CODEOWNERS for production and access paths.

## Secret management

Do not commit production secrets. Store runtime credentials in environment variables, Kubernetes Secrets, or an external secret manager.

## Forbidden operations

Sentinel must not expose tools for shell execution, SSH, kubectl mutation, terraform apply, or secret reads.

## Approval model

The LLM can create PRs, but humans approve and merge. GitHub Actions and Argo CD perform reconciliation after merge.

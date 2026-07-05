# Implementation Plan

## Phase 1: Bot foundation

- Python package and Dockerfile
- Slack Socket Mode app
- natural-language request runtime
- JSON audit logs

## Phase 2: LLM tool selection

- OpenAI Responses API integration
- MCP-style in-process tool gateway
- no heuristic execution when OpenAI key is missing

## Phase 3: Policy Engine

- Slack user to role mapping
- tool-call authorization
- production PR restriction
- admin-only access PR restriction

## Phase 4: GitHub PR tools

- deploy PR patches Helm values image fields
- restart PR patches restart annotation
- rollback PR patches image fields to target
- access tools patch `access/users.yaml` in reviewable PRs

## Phase 5: Read-only operational APIs

- Argo CD application status
- Argo CD managed resource summary
- Grafana alert reads

## Phase 6: Production hardening

- GitHub App auth instead of broad PATs
- branch protection and CODEOWNERS
- Supabase audit storage
- structured Slack approval UX
- richer access source-of-truth lookup and expiration automation

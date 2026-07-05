# GitHub Actions Specification

## 1. Workflow overview

GitHub Actions is the executor. Sentinel creates PRs, but Actions validates, plans, synchronizes access, publishes images, and records audit events.

## 2. Plan workflow

Trigger: pull request opened, synchronized, reopened against `cluster-config`.

Steps:

1. Checkout PR.
2. Validate YAML and JSON schemas.
3. Render Helm/Kustomize manifests.
4. Run policy checks for target environment and changed paths.
5. Run Argo CD diff in read-only mode if credentials are available.
6. Comment plan result on PR.
7. Write `workflow.plan_completed` audit event.

## 3. Apply workflow

Trigger: push to protected default branch after merge.

Steps:

1. Checkout merged GitOps state.
2. Validate commit provenance and PR approvals.
3. Notify Argo CD by webhook or wait for auto-sync.
4. Observe Argo CD app health.
5. Write audit event.
6. Notify Slack.

## 4. Deploy workflow

Trigger: service repository image build or manually approved dispatch.

Steps:

1. Build container image.
2. Push to GHCR.
3. Generate provenance/SBOM if configured.
4. Optionally ask Sentinel to open deploy PR, or require user command.

## 5. Access workflow

Trigger: changes under `access/**` after merge.

Steps:

1. Validate users, roles, and groups schema.
2. Detect grants, revokes, onboarding, offboarding.
3. Sync GitHub teams.
4. Sync Tailscale ACL inputs or policy repository.
5. Sync Grafana teams and folder permissions.
6. Sync Argo CD RBAC ConfigMap via GitOps.
7. Write audit events per target.

## 6. Approval workflow

Trigger: pull request review submitted, label changed, environment approval.

Responsibilities:

- Enforce required labels such as `risk:production`.
- Require CODEOWNERS or admin review for production.
- Require incident ID for break-glass.
- Block merge if plan failed.

## 7. Rollback workflow

Trigger: rollback PR merge.

Steps:

1. Validate target revision exists in deployment history.
2. Confirm rollback policy.
3. Merge approved GitOps change.
4. Observe Argo CD health.
5. Notify Slack and write audit.

## 8. Audit workflow

Trigger: workflow_run or called by other workflows.

Purpose: send structured workflow events to Supabase. If Supabase is unavailable, upload signed workflow artifact and retry through scheduled job.

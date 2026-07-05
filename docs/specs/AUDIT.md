# Audit Specification

## 1. Requirements

Every meaningful event must be audited. Audit records are append-only and correlated by `request_id`.

## 2. Event types

- `request.received`
- `request.denied`
- `llm.tool_selected`
- `tool.authorized`
- `tool.denied`
- `tool.failed`
- `pr.created`
- `workflow.plan_started`
- `workflow.plan_completed`
- `workflow.apply_started`
- `workflow.apply_completed`
- `argocd.sync_observed`
- `access.synced`
- `notification.sent`
- `incident.break_glass_used`

## 3. Database schema

```sql
create table audit_events (
  id uuid primary key default gen_random_uuid(),
  request_id uuid not null,
  event_type text not null,
  actor_user_id text,
  actor_slack_user_id text,
  actor_github_username text,
  command text,
  environment text,
  service text,
  source_channel text,
  tool_name text,
  pull_request_url text,
  result text not null,
  reason text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index audit_events_request_id_idx on audit_events(request_id);
create index audit_events_created_at_idx on audit_events(created_at desc);
create index audit_events_actor_idx on audit_events(actor_user_id, created_at desc);
```

## 4. Storage timing

- Before policy evaluation: `request.received`.
- After policy result: `request.denied` or `tool.authorized`.
- After PR creation: `pr.created`.
- During CI: workflow events.
- After Argo CD observes desired state: `argocd.sync_observed`.

## 5. Query API

Initial API endpoints:

```text
GET /audit/events?request_id=...
GET /audit/events?actor=...&from=...&to=...
GET /audit/operations/{request_id}
```

Only admins can query all audit events. Users can query their own operation history.

## 6. Slack notifications

High-risk events notify `#devops-approvals`; failures notify `#devops-alerts`; debug-only traces go to `#devops-debug` in non-production.

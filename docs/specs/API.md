# API Specification

Sentinel starts as a Slack-first service, but internal HTTP APIs make health checks, audit lookup, and future web UI integration explicit.

## 1. Health

### GET /health/live

Returns whether the process is alive.

Response:

```json
{"status": "ok"}
```

### GET /health/ready

Returns whether required dependencies are reachable enough to accept work.

Response:

```json
{
  "status": "ok",
  "checks": {
    "github": "ok",
    "supabase": "ok",
    "openai": "configured",
    "slack": "ok"
  }
}
```

## 2. Slack endpoints

Socket Mode is preferred, so no public Slack ingress is required.

If HTTP mode is enabled later:

### POST /slack/events

Validates Slack signature, timestamp, and retry headers. Routes app mentions and event callbacks.

### POST /slack/interactions

Validates Slack signature and routes button clicks, modals, and confirmations.

## 3. Audit APIs

All audit APIs require admin authentication through the future web UI or an internal service token.

### GET /audit/events

Query parameters:

- `request_id`
- `actor`
- `service`
- `environment`
- `from`
- `to`
- `event_type`

Response:

```json
{
  "items": [
    {
      "id": "uuid",
      "request_id": "uuid",
      "event_type": "pr.created",
      "actor_github_username": "example-dev",
      "service": "api",
      "environment": "staging",
      "result": "success",
      "created_at": "2026-07-04T10:00:00Z"
    }
  ],
  "next_cursor": null
}
```

### GET /audit/operations/{request_id}

Returns the full operation timeline for one request.

## 4. Internal operation APIs

These are optional future APIs for a web UI. The Slack path can use the same service layer without exposing these endpoints publicly.

### POST /operations/preview

Creates a policy-checked preview without making a PR.

### POST /operations/pr

Creates a PR for a prevalidated operation. This endpoint must require a trusted authenticated session and must run the same policy engine as Slack.

## 5. Error model

```json
{
  "error": {
    "code": "policy_denied",
    "message": "command requires elevated role",
    "request_id": "uuid"
  }
}
```

Do not include secret values, raw provider responses containing credentials, or unredacted manifests in API errors.

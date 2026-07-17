# Slack Specification

Sentinel receives Socket Mode `app_mention` and `member_joined_channel` events. The Slack app uses `app_mentions:read`, `channels:read`, `groups:read`, `chat:write`, `commands`, and `reactions:write`; it does not require Slack email scopes.

DM handling is disabled by default. Set `SENTINEL_SLACK_ALLOW_DMS=true` only when DM access is intentional. Private control and alert channels must explicitly invite the bot.

The one configured onboarding channel is an explicit read-only trust boundary. Members may ask operational and AST-validated production database read questions there without a registered role. Outside it, unknown users receive no default role and are denied before the LLM runs. PR-writing tools always require their existing registered role.

On join, Sentinel replies under a configured fixed welcome parent and asks unregistered members to run `/onboarding <Tailscale email>`. This deterministic handler does not invoke the LLM. It links the explicit Tailscale claim to the Slack user in a default `gui-user` draft PR, rejects conflicting mappings, and reuses a deterministic branch when a request is already pending. A human reviews and merges the PR.

Tool calls are authorized again after service and environment are resolved from server-side configuration.

Replies contain the actual PR URL, Argo CD resources/applications/Pod logs, Grafana alert summaries, or a denial reason. Multiple read-tool results are joined instead of being reduced to a count.

Reaction status requires the Slack bot scope reactions:write. Requests receive hourglass_flowing_sand while processing, then white_check_mark on success or x on denial/failure. Reaction API failures never suppress the text response.

## Message layout

Interactive Sentinel messages use one Block Kit contract rather than legacy attachments:

- a plain-text header identifies the status and feature;
- a mrkdwn section contains the escaped message body;
- pull requests use a button instead of a bare preview card;
- link and media unfurls are disabled;
- top-level fallback text disables mrkdwn parsing;
- database metadata is rendered as fields and the table remains in code blocks.

Database tables are split across section blocks below Slack's 3,000-character block limit. The
fallback contains only the row summary, avoiding a second copy of a potentially large result.

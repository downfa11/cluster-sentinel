# Slack Specification

Sentinel receives Socket Mode events and handles `app_mention` in configured control channels. The Slack app requires `app_mentions:read`, `chat:write`, `users:read`, and `users:read.email`; the repository app manifest declares the event subscription.

DM handling is disabled by default. Set `SENTINEL_SLACK_ALLOW_DMS=true` only when DM access is intentional. Private control and alert channels must explicitly invite the bot.

For every request Sentinel resolves the Slack user from the GitHub-managed access file. Unknown users receive no default role and are denied before the LLM runs. Tool calls are authorized again after service and environment are resolved from server-side configuration.

Replies contain the actual PR URL, Argo CD resources/applications/Pod logs, Grafana alert summaries, or a denial reason. Multiple read-tool results are joined instead of being reduced to a count.

Reaction status requires the Slack bot scope reactions:write. Requests receive hourglass_flowing_sand while processing, then white_check_mark on success or x on denial/failure. Reaction API failures never suppress the text response.

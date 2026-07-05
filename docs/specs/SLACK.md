# Slack Specification

Sentinel is a natural-language Slack agent, not a command-first bot.

## Inputs

- Direct message to the bot
- Bot mention in a channel

## Example messages

```text
api를 staging에 ghcr.io/acme/api:v1.2.3 버전으로 올려줘
Restart api in staging
api production 상태 확인해줘
Show Grafana alerts for api
alice@example.com에게 operator 권한 부여 PR 만들어줘
```

## Behavior

1. Slack sends an event through Socket Mode.
2. Sentinel verifies the Slack event through Slack Bolt.
3. Sentinel maps Slack user ID to roles.
4. OpenAI selects an MCP tool.
5. Policy Engine authorizes the exact tool call.
6. Sentinel executes the tool.
7. Sentinel replies with PR URL, status, or denial reason.

## Channels

`SENTINEL_SLACK_CONTROL_CHANNELS` can restrict where Sentinel responds. DMs are accepted by default.

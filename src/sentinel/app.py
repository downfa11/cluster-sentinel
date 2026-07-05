from __future__ import annotations

import argparse
import logging

from sentinel.env import load_dotenv


def main() -> None:
    load_dotenv()

    from sentinel.config import Settings
    from sentinel.runtime import SentinelRuntime

    parser = argparse.ArgumentParser(description="Sentinel Slack bot")
    parser.add_argument("--dry-run-command", help="Run one natural-language request locally without connecting to Slack")
    parser.add_argument("--user", default="local-user", help="Slack user ID for dry-run mode")
    parser.add_argument("--channel", default="local-channel", help="Slack channel ID for dry-run mode")
    args = parser.parse_args()

    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    if args.dry_run_command:
        runtime = SentinelRuntime(settings)
        result = runtime.handle_text(args.dry_run_command, args.user, args.channel)
        print(runtime.format_result(result))
        return

    from sentinel.slack.app import SentinelSlackBot

    SentinelSlackBot(settings).start()


if __name__ == "__main__":
    main()

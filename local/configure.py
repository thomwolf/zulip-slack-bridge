"""Prepare bridge settings for the local fixture; Slack credentials remain user-supplied."""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    """Create private local configuration without replacing existing settings."""
    os.umask(0o077)
    credentials = json.loads((ROOT / "credentials.json").read_text())
    config = ROOT / "bridge.local.toml"
    env = ROOT / ".env.local"
    if not config.exists():
        config.write_text(
            'database = "bridge.sqlite"\nfeed_topic = "Slack feed"\n\n'
            '[slack]\nteam_id = "T_REPLACE_ME"\nchannel_id = "C_REPLACE_ME"\n\n'
            '[zulip]\nsite = "https://zulip.localhost:8443"\n'
            f"channel_id = {credentials['channel_id']}\n"
        )
    if not env.exists():
        values = {
            "SLACK_BOT_TOKEN": "xoxb-replace-me",
            "SLACK_APP_TOKEN": "xapp-replace-me",
            "ZULIP_BOT_EMAIL": credentials["bot"]["email"],
            "ZULIP_API_KEY": credentials["bot"]["api_key"],
            "ZULIP_CA_BUNDLE": str(ROOT / "tls/zulip.combined-chain.crt"),
        }
        env.write_text(
            "\n".join(f"{key}={json.dumps(value)}" for key, value in values.items()) + "\n"
        )
    print("Local bridge files prepared. Fill in Slack IDs/tokens before starting live forwarding.")


if __name__ == "__main__":
    main()

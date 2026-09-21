---
title: Zulip Slack Bridge
emoji: 🔗
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
suggested_hardware: cpu-upgrade
---

# Zulip–Slack bridge

Connects configured Slack channels to Zulip channels using bot accounts and durable Turso storage.

Put channel IDs and the Zulip URL in `bridge.toml` (see `bridge.multi.example.toml`). Store `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `ZULIP_BOT_EMAIL`, `ZULIP_API_KEY`, and the configured Turso credentials in **Space Secrets**. Never put credentials in TOML.

Set the Space variable `BRIDGE_ENABLED=1` to enable forwarding after stopping other bridges that use the same Slack app. With this variable absent, the Space only performs setup checks. Live mode requires Turso for every pair.

`/healthz` reports container liveness. `/readyz` returns 200 only when the Slack socket and all Zulip receivers are connected; otherwise it returns 503. The page exposes no message content or credentials.

After a restart, inspect durable status before setting `BRIDGE_ACCEPT_GAP=1`: messages sent during downtime are not automatically backfilled. Remove that variable after recovery if future restarts should require acknowledgement. A hard stop may leave a durable database owner claim; verify the old process is stopped before releasing it with the documented CLI recovery command. Do not release ownership automatically.

Paid hardware and sleep settings control availability; deployment alone does not configure always-on billing. See the [source and installation guides](https://github.com/thomwolf/zulip-slack-bridge).

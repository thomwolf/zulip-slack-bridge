# Hugging Face Spaces deployment

The bridge runs in the private Docker Space [science/zulipbridge](https://huggingface.co/spaces/science/zulipbridge). Its front page shows forwarding status and configuration links. A Space marked RUNNING only confirms that the container is running; `/readyz` must return HTTP 200 with `forwarding: true` for forwarding readiness.

## Configure

1. Invite the Slack bot to each paired Slack channel and subscribe the Zulip bot to each paired Zulip channel. See [Slack setup and testing](live-testing.md) and [Zulip setup](zulip-setup.md).
2. Put `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `ZULIP_BOT_EMAIL`, `ZULIP_API_KEY`, `TURSO_DATABASE_URL`, and `TURSO_AUTH_TOKEN` in [Space Settings → Secrets](https://huggingface.co/spaces/science/zulipbridge/settings). Never put credentials in repository files.
3. Edit [bridge.toml](https://huggingface.co/spaces/science/zulipbridge/blob/main/bridge.toml). Set the workspace, Zulip site and channel pairs, with `storage_backend = "turso"`. Each pair needs its own database; use the credential variable names specified by its `turso_url_env` and `turso_token_env`. See [multiple pairs](multiple-pairs.md).
4. Set the Space variable `BRIDGE_ENABLED=1` to run live. Without it, the container serves setup checks only. `BRIDGE_ACCEPT_GAP=1` acknowledges that messages sent during downtime are not backfilled.
5. Use one replica and one worker per database. The current Space uses CPU Upgrade with sleep disabled. Stop any local worker before starting the hosted bridge.

Committing a repository edit automatically rebuilds and restarts the Space. The TOML is read on startup; it is not hot-reloaded. Check the front page after the rebuild completes.

## Changing an established pair

The database binds its state to the Slack channel, Zulip site/channel, feed topic and bot identities. Changing those values in TOML alone does not migrate existing mappings: startup refuses the mismatch. Keep the old database intact, and arrange an explicit migration or start the new pair with a fresh database. Do not simply rewrite the stored identity: old replies and edits could otherwise target the wrong channel.

## Deployment and recovery

Deploy `Dockerfile`, `.dockerignore`, `pyproject.toml`, `uv.lock`, `src/chat_bridge/*.py`, and the non-secret `bridge.toml`. Use `deploy/huggingface/README.md` as the Space root README. Preserve configuration and secrets on code updates. Never upload local credentials, databases, logs or backups.

Pause and confirm the old worker has stopped before manually releasing a stale database ownership claim. Turso retains mappings and delivery journals across restarts. See [connection recovery](connection-recovery.md) for retries, uncertain sends and remaining limitations.

`/healthz` reports container liveness; `/readyz` reports connection and delivery readiness. The front page does not expose chat messages or credentials.

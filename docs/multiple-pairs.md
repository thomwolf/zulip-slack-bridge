# Multiple channel pairs in one bridge

One process can now connect several one-to-one pairs within **one Slack workspace and one Zulip organization**, using the same Slack app and Zulip Generic bot. Separate workspaces/organizations and one-to-many forwarding are not included in this version.

1. Invite the Slack bot to each Slack channel.
2. Subscribe the same Zulip bot to each corresponding Zulip channel.
3. Copy [bridge.multi.example.toml](../bridge.multi.example.toml) to your private `bridge.toml` and fill in the workspace, organization URL, and channel IDs. Each pair has a unique `id`, feed-topic name, and database.
4. Check and start all pairs together:

```sh
uv --no-config run --env-file .env chat-bridge --config bridge.toml check
uv --no-config run --env-file .env chat-bridge --config bridge.toml run
```

Existing single-pair configuration still works unchanged. When converting an existing pair, retain its database path, channel IDs, bots, and feed topic to keep its message mappings. Stop its old process before starting the combined bridge. Do not run a separate process using the same Slack app: Slack distributes Socket Mode events across connections rather than broadcasting each event to all of them.

## Isolation

There is one Slack Socket Mode connection and one routed acknowledgement per envelope. Each pair has its own worker, delivery journal, routing state, and Zulip event queue. The Zulip queues share the bot identity, while filtering messages by channel and tracking edits/deletes/reactions only for that pair. A held message or stopped worker in one pair does not block the others. Shared platform rate limits and authentication outages can still affect multiple pairs.

The entire configuration must pass preflight before startup. Duplicate channel IDs, pair IDs, or databases are rejected. Moving a Zulip message out of its mapped channel retains the existing withdrawal behavior; it does not automatically import that message into another pair.

## Status and recovery

```sh
uv --no-config run --env-file .env chat-bridge --config bridge.toml status
uv --no-config run --env-file .env chat-bridge --config bridge.toml --pair research status
uv --no-config run --env-file .env chat-bridge --config bridge.toml --pair research retry EVENT_KEY
```

Stop the combined process before using recovery commands that change a pair's database. Start all pairs together again with `run --accept-gap` after reviewing status. `--pair` selects status/recovery operations; it cannot run a subset of a multi-pair configuration, because that would consume Slack events intended for the omitted pairs.

## Turso

Set top-level `storage_backend = "turso"`. This first implementation uses **one Turso database per pair**, so the existing journals and ownership locks remain isolated. Bot credentials remain shared. By default, a pair named `research` reads:

```dotenv
TURSO_RESEARCH_DATABASE_URL=libsql://your-research-database-your-org.turso.io
TURSO_RESEARCH_AUTH_TOKEN=replace-me
```

Override the environment-variable names with `turso_url_env` and `turso_token_env` on a pair if needed. For an existing Turso pair you can keep `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` this way. Different variable names pointing to the same Turso host are rejected at group startup. Put all database and bot credentials in Space Secrets when hosting. The deployed Space still uses its setup-only entrypoint; live hosted orchestration remains separate work.

## Local validation

Run `uv --no-config run python local/multi-smoke.py` to exercise the existing local Zulip fixture. It creates/reuses `bridge-multi-a` and `bridge-multi-b`, subscribes the same test bot and fixture users, and posts synthetic messages. Slack is simulated; no additional live Slack connection is opened.

Validated: simultaneous pairs, identical Slack timestamps isolated by pair, correct destination channels, first-reply topic promotion, reverse Zulip delivery, edit isolation, and continued delivery in pair B while pair A is deliberately held. The test uses temporary databases and removes its event queues afterward; the synthetic Zulip messages remain visible for inspection. It does not migrate or change the existing live pair.

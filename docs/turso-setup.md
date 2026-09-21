# Turso storage setup

Turso support is experimental until the hosted probe and a disposable channel-pair test pass. Local SQLite remains the default. **Do not switch the current live bridge to an empty remote database:** that would lose its existing message mappings.

## 1. Create a database

Create a free account at [Turso](https://turso.tech/), then create a dedicated **libSQL** database for the bridge. This implementation uses the `libsql` driver, not the newer Turso engine. Choose a region near the future Space and verify it is covered by [Turso's durability guarantees](https://docs.turso.tech/cloud/durability).

Generate a read/write token scoped to this database. Save both values in the ignored `local/.env.local` file for testing:

```dotenv
TURSO_DATABASE_URL=libsql://your-database-your-org.turso.io
TURSO_AUTH_TOKEN=replace-me
```

When deploying, put these in Hugging Face **Space Secrets**. Never put the token in TOML, Git, an issue, or a public Space variable.

## 2. Test the hosted database first

```sh
uv --no-config sync --frozen
uv --no-config run --env-file local/.env.local python local/turso-smoke.py
```

This uses a uniquely named temporary probe table with synthetic text. It checks commit, rollback, and persistence after closing and reopening the connection, then removes the table. It does not touch bridge tables or chat APIs. If connectivity prevents cleanup, a table with the `bridge_probe_` prefix may remain; remove it once connectivity returns.

## 3. Select the backend for a disposable pilot

Copy `bridge.example.toml` into a separate ignored `bridge.toml`, enter a **new test channel pair**, and set this at the top level (before the `[slack]` section):

```toml
storage_backend = "turso"
```

The local `database` path is unused with this backend. The remote database is authoritative; there is no offline local-write fallback or periodic snapshot upload.

Run:

```sh
uv --no-config run --env-file local/.env.local chat-bridge --config bridge.toml check
uv --no-config run --env-file local/.env.local chat-bridge --config bridge.toml run
```

`check` still needs all chat credentials. Store initialization creates the bridge schema. `status` needs only Turso credentials, not Slack or Zulip access:

```sh
uv --no-config run --env-file local/.env.local chat-bridge --config bridge.toml status
```

Restart acknowledgement (`run --accept-gap`) remains explicit: database persistence does not recover platform events missed while offline.

## Ownership and failures

The remote database holds one persistent worker claim. Normal shutdown releases it. On an ambiguous database error or abrupt process termination, the claim remains and prevents a second worker from starting. Claims deliberately do not expire: a stalled old worker must not overlap a replacement. Database errors poison the connection; the worker must reconnect and inspect state, never assume a timeout means rollback.

After checking that **the previous worker and all of its receiver threads are stopped**, read `worker_owner.owner` from `status` and release exactly that claim:

```sh
uv --no-config run --env-file local/.env.local chat-bridge --config bridge.toml \
  release-owner OWNER_FROM_STATUS --confirm-worker-stopped
```

Then inspect pending/uncertain operations using the existing recovery guide before restarting. Never release another active worker's claim. Use one Space replica. This protects shared state but is not an exactly-once guarantee for external chat APIs.

## Before migrating the existing live bridge

The implementation and fault tests run against SQLite and the actual libSQL driver locally; a successful hosted probe is a separate requirement. The production journal has not been migrated automatically. Stop the old worker, take a consistent private SQLite backup, import all five journal/state tables into an empty remote database, and compare identities, counts, and mappings before enabling the replacement. That import needs an explicit migration procedure; do not casually point the existing bridge at a fresh database or copy SQLite files onto HF buckets.

Current HF Docker entrypoint remains setup-only. Always-on Space provisioning, online Zulip access, hosted validation, and migration are separate remaining steps.

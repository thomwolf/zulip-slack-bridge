# Durable storage options for a Hugging Face Space

Research date: 21 September 2026. This is a design comparison based on provider documentation and the bridge code, not a completed hosted-database benchmark. No database account or paid resource was provisioned.

## Recommendation

Prototype a **remote-primary Turso database over HTTPS**, retaining local SQLite for local installations. It is the closest match to our current SQL and transaction structure. Confirm which Turso engine/driver is selected: current documentation distinguishes the newer Turso engine (`turso_serverless`) from libSQL (`libsql`). Do not assume either is a drop-in replacement for every Python `sqlite3` behavior we use.

The Space would hold the stateless worker and credentials; the remote database would hold the authoritative event journal and message mappings. Every restart would reconnect to that database. Local files could be disposable.

## What the bridge needs

The five existing tables hold metadata/routing state, incoming events, send correlation identifiers, Slack envelope deduplication, and outgoing operations. We need:

- Atomic event ingestion and cursor/envelope updates before acknowledging receipt.
- Confirmed durable intent before sending a message to Slack or Zulip.
- Consistent reads when deciding whether a send has already happened.
- A single active worker across deployments, including overlapping old/new containers.
- Recovery from an uncertain database commit, without assuming that a timeout means rollback.

Database durability cannot make a chat API send atomic with a database transaction. The current uncertain-send reconciliation and operator review remain necessary. It also does not recover Slack events missed during downtime.

## Options

| Option | Fit for this bridge | Main work / limitation |
| --- | --- | --- |
| Turso, direct remote access | Best initial candidate: SQLite-compatible SQL, Python drivers, HTTPS | Adapt transaction and row interfaces; handle network failures; verify chosen driver and engine; add worker ownership |
| Supabase Postgres through HTTPS RPC | Good alternative when the team already uses Supabase | Move atomic store operations into database functions; migrate SQLite SQL/data to Postgres |
| Neon Postgres over HTTPS/WebSockets | Good Postgres foundation | Official serverless driver is JavaScript/TypeScript; our Python bridge needs an adapter/service or suitable verified client, plus SQL migration |
| Cloudflare D1 behind an authenticated Worker | Viable SQLite-semantics alternative | Extra service to deploy; redesign store operations around server-side batches and conditional SQL |
| HF bucket / periodic SQLite backups | Useful for backups, unsuitable as the current live database | A mounted bucket is not the required transactional filesystem; asynchronous backups can lose acknowledged events |

### Turso

Turso documents [direct Python remote access](https://docs.turso.tech/sdk/python/quickstart) and an [HTTPS transaction protocol](https://docs.turso.tech/sdk/http/reference). The latter supports interactive transactions with a five-second transaction window, so chat API calls must remain outside database transactions. Our current store already separates those calls.

For the applicable current AWS-backed plans, Turso documents that [commits are acknowledged after durable storage](https://docs.turso.tech/cloud/durability). Verify the provisioned database/region is covered by those guarantees. Its [free tier](https://turso.tech/pricing) currently includes 5 GB storage, 500 million rows read and 10 million rows written per month, with one day of point-in-time restore. This appears ample for a one-channel pilot after polling/index changes, but is not a measured usage estimate or an availability SLA. Team ownership and recovery-retention requirements may affect plan choice.

Use direct remote writes for the first implementation. A local database with later `push()` is a different durability model: a local commit alone would not justify acknowledging an incoming event.

### Supabase

Use [database functions](https://supabase.com/docs/guides/database/functions) called over HTTPS to encapsulate complete transactional operations. Independent REST calls are not a multi-step transaction. Direct PostgreSQL ports do not fit Spaces' documented egress ports without another access path.

[Pro starts at $25/month](https://supabase.com/pricing), including compute credit sufficient for one Micro instance. The free plan is useful for testing but [low-activity projects can pause](https://supabase.com/docs/guides/platform/free-project-pausing). It is more migration work than Turso, but attractive if the organization already maintains Postgres/Supabase.

### Neon and D1

Neon's [official serverless driver](https://github.com/neondatabase/serverless) supports HTTPS for single queries and non-interactive transaction batches, with WebSockets for interactive transactions. This is workable but adds a language/driver boundary for this Python project. No current Neon price estimate is asserted here.

D1 offers [SQLite semantics and HTTP access](https://developers.cloudflare.com/d1/), with a [batch-oriented Worker API](https://developers.cloudflare.com/d1/worker-api/d1-database/). The proposed design would expose specific authenticated store operations through a Worker, rather than a public arbitrary-SQL endpoint. Its [free plan has daily quotas](https://developers.cloudflare.com/d1/platform/pricing/) that stop queries when exhausted; database usage and Worker usage must both be considered.

### HF-only storage

The [bucket mount implementation](https://github.com/huggingface/hf-mount#best-for--not-for) explicitly excludes strong-consistency workloads and cross-client locking. Do not place the existing SQLite WAL database on it. Building a durable object-store transaction/journal layer might be possible with appropriate conditional-write guarantees, but that is a separate storage-system project, not a simple bridge deployment. Buckets can hold private consistent backups once a primary database is in place.

## Changes required whichever remote provider we choose

1. Separate the store interface from the SQLite implementation, keeping the current backend working.
2. Replace the 0.2-second idle polling loop with an event wake-up and bounded retry timers. The current loop can approach 432,000 iterations/day, each doing at least two database queries. Index the unfinished-event lookup; avoid scanning all completed history.
3. Add database-backed worker ownership and fencing checks. A local `flock` cannot coordinate separate Space replicas or overlapping deployments. A lease alone does not make external Slack/Zulip writes exactly-once; retain write-ahead intent, ownership checks, and uncertain-write recovery.
4. Treat database outages as a reason to pause forwarding. On an ambiguous commit, reconnect and inspect durable operation identifiers before proceeding.
5. Import a consistent SQLite snapshot only after stopping the old worker; verify table counts, identity and existing message mappings. Use a fresh identity instead if the channel pair changes.
6. Test duplicates, rollback, commit-response loss, restart, worker overlap, and editing an existing mirrored message after migration. Measure ingestion latency against Slack's acknowledgement deadline.
7. Keep the database token in Space Secrets and use a token limited to this database. Keep the database and backups private because pending events and routing state can contain chat text.

The next practical step is a disposable Turso proof of concept using synthetic events, with the same store contract tests as SQLite. Only after that should we migrate the live journal and enable the hosted worker.

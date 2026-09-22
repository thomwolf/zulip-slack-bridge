# Connection recovery

The bridge distinguishes safe retries from writes that might already have succeeded.

- Reactions retry automatically, including after a process restart. Duplicate add/remove responses count as success.
- Edits, topic moves and deletes record a hash of the destination before the write. After a lost response, the bridge reads the destination: an already-applied change completes without another write; unchanged state permits a retry; conflicting changes remain held. Read failures retry without writing. Existing permission and age policies still apply.
- New message sends use durable correlation receipts: Slack bot metadata and a receipt parameter in the existing source link on Zulip (the visible author label stays unchanged). Queue echoes or destination history can confirm delivery after a lost response. Otherwise, after at least two minutes, two complete history scans at least 30 seconds apart must find no receipt before the bridge retries using the same receipt. Repeated failed attempts increase the grace period up to 15 minutes. Inaccessible or incomplete history and multiple receipt matches leave the send held. History scans are bounded to ten pages.
- This is an at-least-once recovery policy, not an exactly-once guarantee: an exceptionally late platform write, or removal of its receipt, can still cause a duplicate. Neither Zulip local IDs nor these receipt markers are server-side idempotency keys. Legacy unmarked sends and Zulip sends without a source link remain held for manual resolution.
- Image metadata/download connection errors retry before uploading. If upload confirmation is lost, the bridge delivers the text with an attachment notice pointing to the original instead of uploading again. An unreferenced private upload may remain on the destination.
- Held or delayed events block dependent messages within their conversation. Independent conversations continue unless a create has begun remote operations: such creates and topic moves retain a conservative ordering barrier until their routing changes commit. This prevents replies to partially created topics from starting duplicate threads. Selection examines up to 1,000 unfinished events per cycle.
- `/readyz` reports non-ready while deliveries are held or retrying, even if both platform connections are up. It does not send notifications on its own.

Database errors remain fail-closed. An ambiguous Turso commit stops the worker and retains its ownership claim. An operator must verify the old process stopped, inspect durable state, and release the claim before restarting. Automatically stealing a claim or retrying an unknown database commit could duplicate writes.

These mechanisms recover work already received by the bridge. They do not backfill messages sent while it was offline. The configured restart-gap setting controls whether forwarding may resume despite that gap.

No API keys or additional message bodies are stored in the new recovery snapshot table; it contains operation kinds and hashes of destination state.

Transient platform preflight failures retry with exponential backoff, capped at 60 seconds unless a platform requests a longer delay. Invalid configuration, authentication rejection, ambiguous database failures and ownership conflicts still stop safely.

Full-text correction notices are tracked as additional copies and withdrawn or redacted when their source is deleted. If platform policy refuses both operations, a visible notice identifies the remaining correction. Notices produced by older releases require a one-time mapping backfill before this tracking applies.

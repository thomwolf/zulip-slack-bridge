# Connection recovery

The bridge distinguishes safe retries from writes that might already have succeeded.

- Reactions retry automatically, including after a process restart. Duplicate add/remove responses count as success.
- Edits, topic moves and deletes record a hash of the destination before the write. After a lost response, the bridge reads the destination: an already-applied change completes without another write; unchanged state permits a retry; conflicting changes remain held. Read failures retry without writing. Existing permission and age policies still apply.
- Message sends use durable correlation receipts. Zulip queue echoes and Slack bot metadata echoes can confirm delivery; Slack history lookup also searches up to ten pages. Failure to find a receipt does not prove the send failed, so an ambiguous send remains held rather than creating a duplicate. Queue expiration, inaccessible history, and missing metadata can require manual resolution.
- Image metadata/download connection errors retry before uploading. If upload confirmation is lost, the bridge delivers the text with an attachment notice pointing to the original instead of uploading again. An unreferenced private upload may remain on the destination.
- Held or delayed events block dependent messages within their conversation. Independent conversations continue. Topic moves retain a conservative ordering barrier. Selection examines up to 1,000 unfinished events per cycle.
- `/readyz` reports non-ready while deliveries are held or retrying, even if both platform connections are up. It does not send notifications on its own.

Database errors remain fail-closed. An ambiguous Turso commit stops the worker and retains its ownership claim. An operator must verify the old process stopped, inspect durable state, and release the claim before restarting. Automatically stealing a claim or retrying an unknown database commit could duplicate writes.

These mechanisms recover work already received by the bridge. They do not backfill messages sent while it was offline. The configured restart-gap setting controls whether forwarding may resume despite that gap.

No API keys or additional message bodies are stored in the new recovery snapshot table; it contains operation kinds and hashes of destination state.

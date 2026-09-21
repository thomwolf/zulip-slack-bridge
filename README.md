# Zulip–Slack bridge

A first experimental Python implementation for one Slack channel and one Zulip channel. We host the bridge; our friends on Zulip only create a normal Generic bot and add it to their channel.

**Status: experimental pilot tested with real Slack and a local Zulip 12.2 server.** Posts, threads, edits, reactions, images, and formatting have been exercised; this is not a production-ready, lossless sync service. See [local test results](local/TEST-RESULTS.md).

Start with the [simple install guide](INSTALL.md). The [local server guide](local/README.md) covers the isolated Zulip fixture.

For hosting on Hugging Face, see [Spaces deployment findings](docs/huggingface-spaces.md). The prepared container currently serves setup status only; durable database support and an online Zulip server are still required for live forwarding.

## Try the offline example

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv --no-config sync --frozen
uv --no-config run chat-bridge demo
```

The demo uses the real conversation engine and a temporary SQLite database, with fake remote APIs. It forwards messages, promotes a Slack thread, edits the parent, aggregates reactions and starts a conversation from Zulip. It performs no network calls and needs no credentials. Duplicate input delivery is intentionally exercised.

The `--no-config` flag avoids an existing user-level uv configuration on the development machine that is incompatible with its installed uv version. It does not modify that configuration. On other machines, ordinary `uv sync` / `uv run` also work with a compatible configuration.

## What is implemented

- A configurable one-channel boundary and bot-only authentication.
- Top-level Slack messages and ordinary Zulip feed messages mirrored both ways.
- First Slack reply copies the original into a named Zulip topic before adding the reply. Its feed copy becomes a linked navigation pointer; if editing is forbidden or the original belongs to a Zulip user, the feed original remains with a separate pointer. Later edits, deletions and reactions follow the topic copy. Existing reactions are represented there by the bridge bot.
- New Zulip topics create a linked Slack title message and post the first message as its first reply. Complete topic renames update the title. Message interactions target replies; title reactions have no Zulip counterpart. Existing threads and feed messages keep their current layout.
- New Zulip topics become Slack threads; moving one feed message into a fresh topic binds its existing Slack parent.
- Complete known-topic renames maintain the mapping. Ambiguous merges and partial moves are flagged instead of routed into a guessed conversation.
- Source edits update bot copies in place; observed source deletions remove copies, or attempt a neutral deletion notice if removal is forbidden. A failure remains visible.
- Destination deletion suppresses further updates and never deletes the human original. An unexpected edit made using the bot’s own credentials is also flagged; ordinary humans cannot edit bot content. Moving a message out of the shared Zulip channel stops exporting later edits/reactions on that message.
- A small explicit standard emoji set; multiple remote people produce one bot reaction. Local reactions remain separate. Supported Slack emoji with skin tones collapse to the base emoji on Zulip; custom emoji remain unsupported.
- Durable SQLite input and operation journals, duplicate input detection, rate-limit retries, and explicit uncertain-write holds after interrupted requests.
- Non-posting identity/membership/policy preflight, local status, positive correlation recovery for sends, and operator resolution for unresolved writes.
- Real SDK adapters and receiver code: Slack Socket Mode and Zulip event polling. Incoming work is committed before Slack acknowledgement / Zulip cursor advancement.

Messages use visible author/platform labels and source links. Named links, bold, italic, strikethrough (including combinations), inline/fenced code, lists and quotes are converted between the platforms. Slack structured formatting is preferred over its text fallback; Zulip Markdown is parsed into native Slack rich text. Mentions remain inert. Underline, tables, math, spoilers and other platform-specific widgets do not have full-fidelity conversion. Uploaded PNG/JPEG/GIF images (up to 10 MiB each, five per message) are copied into the destination platform. Slack displays them as image blocks on the same mirrored message; Zulip displays uploaded-image links inline. Unsupported attachments retain an explicit original-message fallback. Source links require access to the original platform.

## Live test setup

Start with a [Zulip Cloud demo organization](https://zulip.com/help/demo-organizations) and a disposable Slack channel whose members know that messages are shared. No historical import is performed. Use Docker only to reproduce the friends’ particular self-hosted version. See [live smoke tests](docs/live-testing.md).

### Slack (our side)

1. Create a Slack app from `slack-app-manifest.json` in the test workspace.
2. Install it and save its bot token (`xoxb-…`). Workspace policy may require approval.
3. Generate an app-level token (`xapp-…`) with `connections:write`; Socket Mode is enabled by the manifest.
4. Invite “From Zulip” to the single public test channel.
5. Copy the workspace and channel IDs into the local configuration.

The supplied manifest is for a public Slack channel. A private-channel pilot needs `groups:history`, `groups:read`, and the `message.groups` event instead, plus membership and a fresh installation approving the changed scopes. That setup is not separately live-validated.

### Zulip (our friends' side)

Follow `docs/zulip-setup.md`. The operator copies the bot email/API key from the securely shared zuliprc file into the secret environment file, and the site/channel ID into configuration. The application does not load a personal Zulip config implicitly.

### Operator configuration

```sh
cp bridge.example.toml bridge.toml
cp .env.example .env
```

Edit those files locally. Do not paste real credentials into issues, chat, logs, or the PRD. `bridge.toml`, `.env`, SQLite data and local trust bundles should remain private. The example manifest contains no credentials.

```sh
uv --no-config run --env-file .env chat-bridge check
uv --no-config run --env-file .env chat-bridge run
```

`check` posts no messages and changes no community policy. It briefly creates and deletes a data-only event queue to inspect policy. It checks workspace identity, bot identity, active Slack channel membership, Zulip subscription, named-topic availability, message/topic limits, and edit/delete windows. Group membership is reported where bot access is supported; unknown is not treated as allowed. It does **not** prove that write/move/delete permissions work, especially for old messages. Use the planned live smoke tests before inviting the friends to rely on it.

Both platform connections are outbound: no public webhook server or tunnel is needed. Zulip must be reachable from our host. A local HTTPS Zulip test server can use `ZULIP_CA_BUNDLE`; certificate verification is never disabled. The current CLI uses POSIX file locking and is intended for macOS/Linux.

The database is bound to the selected workspace, site, channel IDs, feed and bot identities. Changing that connection requires a separate database. Run only one process per database; the CLI enforces this with a file lock. Keep the database on persistent local storage, not a network filesystem.

## Late edits, deletions and withdrawals

Existing Zulip policies stay unchanged. Preflight reports actual configured windows in seconds (`null` is unlimited); normal deployments may limit content edits/deletes to 600 seconds. These are policy ceilings, not a guarantee of permission. After an edit is rejected, the bridge posts a short author-attributed notice in the same topic/thread linking to the original. It does not repeat the corrected text. After deletion is rejected, it tries a neutral replacement; if that edit is also rejected, it posts a notice explicitly saying the old copy remains and records an operator-visible issue. Notice failures hold delivery for repair. A notice is not removal of retained content.

A Zulip `delete_message` event can mean deletion or movement into a channel the bot cannot see. For a Zulip-origin message, both withdraw the Slack copy and are recorded as “deleted or moved out of view.” Deleting a bot mirror never deletes its human source. Visible moves out of the shared channel stop subsequent export and flag the conversation. Successful edits of Slack bot copies append `(edited)` because Slack does not display its native edited indicator for bots.

## Failure and recovery behavior

```sh
uv --no-config run chat-bridge status
```

Status is local and credential-free. It reports pending/failed event keys, uncertain operation keys, receiver health and routing issues without message bodies. A single ordered worker deliberately stops delivery at a failed or uncertain event. Ingestion continues. This conservative choice makes the first version easy to reason about; later work should isolate affected conversations so one problem does not delay the whole channel.

Definite rate-limit rejections retry automatically. Permanent failures need operator repair. Stop the process before invoking repair commands.

```sh
uv --no-config run chat-bridge retry EVENT_KEY
```

An API timeout can occur after a write succeeded. Every new send has a random, durable correlation key: Zulip `local_id` on the sending queue, or Slack message metadata. Own-queue Zulip echoes and bounded Slack history/thread lookups can positively confirm delivery and release the held event automatically. Empty results never prove non-delivery. Unresolved writes remain **uncertain**, and `retry` refuses to repeat them. Inspect the destination manually, then record the actual outcome:

```sh
# A send definitely succeeded: supply the real destination message ID.
uv --no-config run chat-bridge resolve OPERATION_KEY --delivered --message-id DESTINATION_ID

# An edit/move/delete/reaction definitely succeeded (no new message ID).
uv --no-config run chat-bridge resolve OPERATION_KEY --delivered

# Use only after verifying that the write did not occur.
uv --no-config run chat-bridge resolve OPERATION_KEY --not-delivered

uv --no-config run chat-bridge retry EVENT_KEY
```

Operation keys appear in `status`. Slack destination IDs are timestamp strings including the decimal part; Zulip IDs are integer strings. Resolution is an operator assertion, not an automatic remote verification. Never mark an uncertain send “not delivered” merely because a request timed out.

A confirmed permission denial used to choose promotion, correction-notice, or deletion fallbacks is preserved on replay, so a partially completed promotion cannot unexpectedly change branches. Complex topic conflicts currently require restoring the original layout and developer-assisted state repair; no general-purpose repair UI is shipped.

### Offline event gaps

Mappings, accepted inputs and confirmed operations survive restarts. Automatic history reconciliation is **not implemented**. Zulip queues can expire, and Slack may omit events while disconnected. The receiver requests a seven-day idle timeout on Zulip feature level 481+, records the effective timeout, and resumes its persisted queue/cursor. Older servers use the server default. Expiry records a gap interval based on the last successful poll; it does not silently create a new queue. The service stops on an expired Zulip queue and requires explicit acknowledgement to restart:

```sh
uv --no-config run --env-file .env chat-bridge run --accept-gap
```

This accepts that offline edits/deletions/reactions may have been missed; it does not backfill them. Every restart after an earlier run requires this flag. In-process Slack reconnection remains SDK-managed and may also have a gap; this is a pilot limitation. Do not advertise lossless synchronization or use this version for important retention/deletion obligations.

## Known gaps versus the full PRD

- Real Zulip smoke tests pass on the local fixture. Live Slack and the complete two-human rehearsal remain untested.
- No historical-parent retrieval. A reply to an unmapped pre-activation parent is held as `missing_thread_parent`, not silently flattened. Start fresh test conversations.
- No automatic outage/history reconciliation or per-conversation failure isolation. Positive send correlation is implemented; uncertain edits/moves/deletes and unmatched sends still need operator review. Slack lookup scans one page of up to 100 messages per held send every 30 seconds; missing permissions, pagination beyond that page, unavailable metadata or lookup failure leave it uncertain. Zulip local IDs are queue-only echoes, not persisted searchable history fields; expired queues can therefore require manual repair.
- Atomic complete known-topic renames, including resolve/unresolve, preserve the thread binding. Partial moves, merges, splits and renamed feed topics can require manual repair.
- Deleting a Slack parent and subsequently replying still needs a real Slack test. Surviving replies are never cascade-deleted by this bridge.
- Preflight reports configured windows and group membership, not a guarantee of effective mutation permission. Membership lookup using bot credentials requires Zulip feature level 496+; older servers report unknown. No mutation probes are performed.
- The roster is initially loaded at connection time; display-name updates are not live-synchronized.
- Limited standard emoji and conservative text rendering; custom emoji, full emoji vocabulary, file copying, native mention pings and notification parity are not implemented.
- SQLite routing state is a JSON snapshot suitable for a small pilot, not an optimized message archive. No automated data retention or state-compaction job yet. Pending payloads/current mirrored text are sensitive local data; completed raw event bodies are cleared, but storage backups and SQLite pages are not securely erased.
- Local fixture setup and API smoke-test scripts are provided. No production deployment bundle, monitoring integration, or hosted service has been set up.

## Tests and code structure

```sh
uv --no-config run pytest
uv --no-config run ruff check .
uv --no-config run ruff format --check .
uv --no-config run pyright
```

- `engine`: platform-independent conversation and interaction rules.
- `store`: durable input, operation results, routing snapshots and conservative replay.
- `events`: envelope normalization and scope filters.
- `adapters`: SDK authentication, preflight, writes and error classification.
- `runtime`: ingestion and single-worker lifecycle.
- `demo`: fake platform endpoints for an executable offline example.

Tests exercise behavior, replay/crash boundaries, private-channel moves, event contracts, mention escaping and SDK request shapes. The fake endpoints are intentionally not evidence of real Slack/Zulip permissions.

The finding-by-finding response is in [review implementation notes](docs/review-implementation.md). The design and validation backlog are in the workspace's `outputs/zulip-slack-bridge` review package. `PRD.md` remains the target specification; this README describes what is actually shipped.

## Image support (first version)

Add `files:read` and `files:write` to the Slack bot scopes and reinstall the app; the manifest includes both. Uploaded images are fetched with source authentication and uploaded privately before posting their destination message. No public sharing URL is enabled.

Caption edits reuse existing uploads. Removing an image from a message removes it from the mirror; deleting the message removes its displayed images. Topic promotion reuses uploaded Zulip images. Uploaded blobs are not garbage-collected by the bridge, and a standalone file-deletion event without a message update is not yet synchronized. External image URLs, SVG, WebP, HEIC, video, documents, and remote/Slack Connect file stubs are not supported in this first version. Zulip downloads currently require same-origin storage without an external redirect; cloud-storage/CDN downloads fall back to the original.

Transfers run outside event callbacks and are journaled. An uncertain upload pauses delivery for operator review instead of blindly uploading again. Uploaded-file operations do not have automatic reconciliation yet: if the outcome cannot be identified, investigate before using `resolve --not-delivered` and retrying; an abandoned upload may remain private on the destination. No raw image data, download URLs, or temporary signed URLs are persisted. The journal retains file IDs and destination upload paths.

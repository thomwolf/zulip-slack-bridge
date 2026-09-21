# Review implementation response — 21 September 2026

Input: `outputs/zulip-slack-bridge/REVIEW.md`. This records code changes and deliberate limits; local Zulip validation is now recorded in [test results](../local/TEST-RESULTS.md); Slack remains simulated. The user selected short late-change notices while preserving existing Zulip policies. The named Slack feed and Zulip move-to-bind feature are retained.

| Finding | Implementation and remaining validation |
|---|---|
| F1: edit/delete windows | Preflight reads actual realm windows and permission-group settings. Rejected edits post an attributed link notice. Rejected deletes attempt neutral replacement, then a notice explicitly stating that the previous content remains. Issues are visible in status; notice failures remain held. Slack `edit_window_closed` follows this path. Ordinary human editing of bot content is no longer claimed; defensive detection of unexpected changes using the bot credential remains. |
| F2: Slack acknowledgement | Already used raw `SocketModeClient`, with durable normalized input before ACK. Added secondary envelope-ID deduplication alongside event-ID deduplication. Store only needed normalized content plus envelope/event identifiers, rather than an additional full raw payload. Commit-failure and redelivery tests cover this. |
| F3: Zulip queue | Already used explicit register/poll and durable cursor, without silent re-registration. Now requests seven idle days on FL 481+, records the effective timeout, filters channel IDs in the receiver, and records a gap interval on expiry. Old servers keep their server timeout. History backfill remains unimplemented and restart requires explicit gap acknowledgement. |
| F4: correlation | Every create, including context/notices, gets a durable random operation token. Zulip sends queue_id/local_id; only an echo from our bot in the configured channel confirms it. Slack sends metadata and checks history or thread replies for matching metadata and bot identity. Positive evidence releases the event without resending. Empty/failed/bounded lookups remain uncertain. Manual controls remain necessary. |
| F5: movement/topic policy | Preflight reads realm/channel move groups and age limit without distinguishing sender ownership. It checks bot group membership where allowed and blocks empty_topic_only. It does not assume a default role proves effective permission. |
| F6: move notices | Uses change_one plus native old-topic notice; disables redundant new-topic notice. Removed bridge breadcrumb sends. Denied moves retain link-only context. Old databases may still contain legacy breadcrumb journal records; these are not re-sent. |
| F7: withdrawal | Zulip-origin delete_message removes the Slack copy and records deleted-or-moved-out-of-view. Destination-copy deletion suppresses forwarding without deleting the human original. Explicit visible out-of-channel moves stop export and flag routing. |
| F8: reactions | Uses Zulip codepoints/user_id and a small explicitly supported Slack vocabulary. Skin-tone suffixes collapse to base codepoints, while per-user variant membership prevents premature removal when one person selected both variants. Full emoji-data coverage remains outside the pilot. |
| F9: rename/resolve | Atomic complete known-topic renames preserve the Slack root. Resolve/unresolve tests verify no Slack posts. Merges and true partial moves remain conflicts; there is no multi-event rename wait. |
| F10: fixture | Cloud demo organization is now the first live-test path. Docker is optional for matching a particular friends’ server version. A later user request authorized a local Colima fixture; see the local test results. No external accounts were created. |
| F11: Slack details/limits | Membership is mandatory. Hidden edit/delete payloads are processed, duplicate known creates ignored, Slack bot edits get explicit edited text. Zulip length limits are read and used. Parent-deletion/reply behavior and Delayed Events remain live experiments; no replay guarantee is claimed. |
| F12: onboarding | Guide explains bot creation/subscription restrictions, stable owner choice, and no demand for policy relaxation. |
| F13: Docker details | Optional runbook now uses compose.yaml and its override example, CERTIFICATES and explicit port replacement. Current bridge still requires HTTPS; the review’s optional plain-HTTP fixture would need separate explicit loopback configuration support. |
| F14: existing bridges | The existing engine remains necessary for threads, edits and reactions. No replacement with the one-way or plain-text bridge. |

## Corrections to the review itself

Zulip's [local_id documentation](https://zulip.com/api/send-message) promises an echo to the sending queue. It does not promise that local_id is stored or returned by history queries. A sender/channel history search alone cannot uniquely identify a timed-out send when identical messages are allowed. We therefore do not use text matching or infer that an empty search authorizes a retry.

The [group-membership endpoint](https://zulip.com/api/get-is-user-group-member) allows bot callers only from feature level 496; earlier versions must report membership as unknown instead of failing onboarding or assuming authorization. Direct memberships in inline group settings can still be evaluated.

Slack lookup uses the documented metadata fields on [history](https://docs.slack.dev/reference/methods/conversations.history/) and [thread replies](https://docs.slack.dev/reference/methods/conversations.replies/). It scans one page per uncertain send, not unlimited history. Real-token permissions and visibility still require the live smoke tests.

## Delivery boundaries

The implementation retains manual uncertain-write recovery, global FIFO holds, and explicit outage-gap acknowledgement. It does not implement full history reconciliation, complete emoji coverage, Slack Delayed Events provisioning, a general topic-repair UI, or a production deploy bundle. Policy reports are a startup snapshot; policy changes during a run are handled through API rejection/fallback, then refreshed on the next preflight.

This update changes rendering and fallback decisions. Use a fresh disposable pilot database if the previous experimental build has unfinished operations; do not erase a live journal to make a pending replay pass. Completed mappings are retained by the additive schema migration.

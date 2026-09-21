# Local validation results — 21 September 2026

The isolated server is running at **https://zulip.localhost:8443**. API requests use the generated certificate with verification enabled. Only the loopback HTTPS port is published.

- Server: Zulip 12.2, feature level 500; official Linux arm64 image.
- Runtime: dedicated Colima profile `zulip-bridge`, 2 CPUs and 4 GiB RAM.
- Accounts: fixture administrator, Alice and Bob as ordinary members, and a Generic bot owned by Alice with member role.
- Test channel: `bridge-test`, ID 4.
- Secrets: private, ignored `local/credentials.json`; never included in this report.
- Image digests: [image-digests.json](image-digests.json).
- Slack: simulated; no workspace credentials supplied.

## Passed against real Zulip

- Slack-to-real-Zulip send
- own-queue correlation echo
- edit
- reaction
- first-reply promotion
- native notice loop prevention
- real Zulip-to-simulated-Slack send and edit events
- real Zulip delete event
- bot mirror deletion
- late edit notice under real default policy
- late delete notice under real default policy

The late-change tests backdated a synthetic test message by 15 minutes; they did not relax the organization’s policies. The configured edit and delete limits were both 600 seconds. The old copy remained visible and notices were posted as designed.

## Bugs found and fixed

1. Zulip requires `notification_settings_null` when an event registration specifies client capabilities. Added it to the live runtime and regression coverage.
2. A stringified numeric channel ID in the server’s queue narrow did not deliver the expected events. Removed that narrow; the bridge still strictly filters every event by configured channel ID. This also avoids a name-based filter becoming stale after channel renaming.
3. System Notification Bot is returned in `cross_realm_bots`, not the ordinary users list. Preflight and registration now load those bots, preventing native move notices from being mirrored back.

Automated suite: 78 passed; lint, formatting and type checks passed. No claims are made here about live Slack, deleted Slack parents, full outage recovery, every permission profile, private-channel withdrawal, or the two-human notification rehearsal.

## Operational notes

The VM could not reach registries directly on this Mac; verified public images were downloaded through the host and loaded into the VM. Colima, Docker CLI, Compose and crane were installed during setup; the final fallback downloader uses curl and does not require crane. The existing default Docker context and VPN settings were not changed. No login-start service was installed.

The server is left running for inspection. Use the [local guide](README.md) to stop or resume it and the [simple install guide](../INSTALL.md) to connect Slack.

## Real Slack image transfer check

The Slack bot has `files:read` and `files:write`. A synthetic PNG was uploaded to
local Zulip, downloaded through its authenticated temporary URL, privately uploaded
to Slack, and displayed in a Slack image block. That same file was then downloaded
with Slack bot authentication, uploaded back to Zulip, and referenced in a rendered
Zulip message. Both directions passed; message IDs are kept only in the ignored local result file.
These are real transport/API checks, not human-authored event tests.

Slack briefly rejects newly uploaded image IDs with `invalid_blocks` before image
processing completes. The bridge now retries only that explicit rejection for recent
uploads (up to 120 seconds), without repeating the upload. Other invalid block errors
remain visible failures. All 108 automated tests, lint and type checking pass.

Repeat this test only when posting synthetic test images to both configured channels
is desired: `uv --no-config run --env-file local/.env.local python local/image-smoke.py`.

## Formatting conversion check

Real API checks passed for Slack structured formatting → Zulip-rendered Markdown,
and Zulip Markdown → Slack native rich-text blocks. The example covers named links,
bold, italic, strikethrough and combined styles, plus lists, quotes and fenced code.
The synthetic examples remain in the test channels (`Formatting test` on Zulip).
Repeat with `uv --no-config run --env-file local/.env.local python local/format-smoke.py`.

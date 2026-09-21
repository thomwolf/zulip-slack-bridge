# Connect one Slack channel to one Zulip channel

We run the bridge. The Zulip community only creates a bot and subscribes it to the shared channel. People keep their existing accounts.

## 1. Prepare Slack

You need permission to install an app in the Slack workspace. Start with a disposable **public** channel, such as `#zulip-test`.

1. Open [Slack’s app dashboard](https://api.slack.com/apps), choose **Create New App → From a manifest**, and select your workspace.
2. Paste the contents of [`slack-app-manifest.json`](slack-app-manifest.json). Create the app.
3. Under **OAuth & Permissions**, install it to the workspace. Save the **Bot User OAuth Token** starting with `xoxb-`.
4. Under **Basic Information → App-Level Tokens**, generate a token named `bridge` with the `connections:write` scope. Save the token starting with `xapp-`. The manifest already enables Socket Mode and the message/reaction events.
5. In `#zulip-test`, invite the bot: `/invite @From Zulip`.
6. Copy the channel ID from the channel’s details. Copy the workspace ID from the Slack web URL (`app.slack.com/client/T…/C…`).

The supplied app needs only bot credentials. It does not need personal Slack authorization or a public webhook URL. Private channels need a different manifest with `groups:history`, `groups:read`, and `message.groups`; use a public test channel first.

## 2. Prepare Zulip

For the Zulip community, send the short [four-step Zulip guide](docs/zulip-setup.md): choose a channel, create a **Generic bot**, subscribe it, and privately share its `zuliprc` file and channel link with the operator. They do not need to run software or extract IDs themselves.

For our local test server, use [local/README.md](local/README.md).

The operator reads the bot email, API key and organization URL from `zuliprc`. The numeric channel ID is in the shared channel URL, before the channel name. Named topics must be enabled and the bot must be able to post and start topics. Keep existing edit/delete policies: blocked updates produce a notice; preflight reports the actual policy windows.

## 3. Configure the bridge

On the machine running the bridge, install [uv](https://docs.astral.sh/uv/getting-started/installation/) and open a terminal in this project directory:

```sh
uv --no-config sync --frozen
cp bridge.example.toml bridge.toml
cp .env.example .env
```

Edit `bridge.toml`:

```toml
database = "bridge.sqlite"
feed_topic = "Slack feed"

[slack]
team_id = "T_YOUR_WORKSPACE"
channel_id = "C_YOUR_CHANNEL"

[zulip]
site = "https://your-organization.zulipchat.com"
channel_id = 123
```

Edit `.env` with the four credentials:

```dotenv
SLACK_BOT_TOKEN=xoxb-your-token
SLACK_APP_TOKEN=xapp-your-token
ZULIP_BOT_EMAIL=your-bot-email
ZULIP_API_KEY=your-bot-key
```

Keep these files private. Do not post tokens into either shared channel. For the local HTTPS server, also set `ZULIP_CA_BUNDLE` to the absolute path of `local/tls/zulip.combined-chain.crt`, and use `https://zulip.localhost:8443` as the site. The local fixture’s bot credentials are saved privately in `local/credentials.json` after provisioning.

## 4. Check and start

```sh
uv --no-config run --env-file .env chat-bridge check
uv --no-config run --env-file .env chat-bridge run
```

`check` verifies the bots, channel membership and Zulip policies without posting messages. Leave `run` open while testing; press **Ctrl-C** to stop. The Mac must stay awake and online for forwarding to work.

Try a message in Slack, a thread reply, a new Zulip topic, an edit, a deletion, and a thumbs-up reaction on each side. Slack standalone messages should share **Slack feed**; a thread reply should create a named topic containing a copy of the original followed by the reply. The feed copy becomes a “Discussion continued” link, or receives a separate link notice when editing is unavailable. A new Zulip topic should create a linked title message in Slack, with the first Zulip message as its first reply. Later messages join that thread; a complete topic rename updates the title. Edits, reactions and deletions target the corresponding replies, not the title.

For local delivery status:

```sh
uv --no-config run chat-bridge status
```

A restart requires explicit acknowledgement that offline events may have been missed:

```sh
uv --no-config run --env-file .env chat-bridge run --accept-gap
```

This does not import missed history. If status reports a held or uncertain write, follow the [recovery instructions](README.md#failure-and-recovery-behavior); do not repeatedly resend it.

## What participants should know

- Posts show the human author, but the copy belongs to a bot. Edit the original where you wrote it.
- A bot reaction means one or more remote people reacted; it is not a vote count. Supported skin tones become the base emoji.
- Mentions do not ping across platforms. Supported images are copied; other files link to the original and may need an account there.
- To reply to a specific Zulip feed message, move that one message into a fresh topic before replying.
- This is an experimental pilot. Do not rely on it for lossless outage recovery or guaranteed removal of old copies.

## Enable images on an existing Slack app

In **OAuth & Permissions → Bot Token Scopes**, add **files:read** and **files:write**, then **Reinstall to Workspace**. Keep the bot token in the bridge environment current; the app-level Socket Mode token does not change.

Test a small PNG/JPEG/GIF upload in each test channel, first without a caption and then with one. The image should appear with the mirrored message. Edit the caption, remove the attachment, and reply in a Slack thread to verify that promotion retains the image. This first version supports up to five images per message and 10 MiB per image. Other files keep an “open original” notice.

# Connect your Zulip channel to Slack

The bridge operator runs the software. Your Zulip community only needs to create a bot, subscribe it to one channel, and privately share its configuration. No server installation or Slack accounts are needed on your side.

## Four setup steps

1. **Choose a channel.** Tell its members which Slack channel will receive their messages and who operates the bridge. Start with a test channel if possible.
2. **Create the bot.** In the web or desktop app, open **gear → Personal settings → Bots → Add a new bot**. Select **Generic bot**, name it **From Slack**, and choose an available username such as `from-slack`. Keep a stable community member as its owner. This bridge needs a Generic bot to receive messages, not an Incoming webhook bot. [Official bot instructions](https://zulip.com/help/add-a-bot-or-integration).
3. **Subscribe it.** Open **gear → Channel settings → All**, select your channel, then open **Subscribers**. Under **Add subscribers**, search for **From Slack** (or its bot email), select it, and click **Add**. If the controls are unavailable, ask a channel or organization administrator to perform this step. The bot must be allowed to post and start named topics; it does not need an administrator role. [Official subscription instructions](https://zulip.com/help/subscribe-users-to-a-channel).
4. **Send the configuration privately.** Return to **Personal settings → Bots** and download this bot’s `zuliprc` configuration. Give the bridge operator that file and a link to the channel using an agreed secure method, such as a password-manager share. The file contains the bot’s API key: never put it in a shared channel, issue, or repository. The operator can extract the site, email, API key and channel ID; you do not need to configure the bridge yourself.

Keep your existing edit/delete policies. The operator runs a connection check and tells you when forwarding is active. Creating the bot alone does not start forwarding.

## Try it together

Post a message in Slack, reply to it in a thread, then create a topic and reply in Zulip. Try editing your own original message, adding a reaction, and uploading a small PNG or JPEG.

- Standalone Slack messages appear in **Slack feed**. The first Slack reply creates a Zulip topic containing a copy of the original followed by the reply. A link in the feed points to that discussion; when an original cannot be edited, the bridge adds a separate link notice.
- A new Zulip topic creates a title message in Slack, with the first Zulip message underneath as its first thread reply. Later messages join that thread.
- Copies belong to the bot and show a linked author name. Edit the original in the app where you wrote it. A bot reaction means one or more remote people reacted, not a vote count. Mentions and personal notification settings do not transfer.
- PNG, JPEG and GIF uploads are copied (up to five per message, 10 MiB each). Other files link to the original, which may require an account there. Some remotely hosted uploads are not yet supported.

For this pilot, avoid merging topics or moving several unrelated feed messages into the same topic. To discuss one feed message from Zulip, move that message into a fresh topic before replying. If a policy prevents changing an old copy, a notice reports that the old content remains. Offline history is not automatically recovered.

## Disconnect

Ask the operator to stop the bridge, or deactivate the bot / rotate its API key in Zulip. A Generic bot’s permissions can extend beyond this one channel; the one-channel forwarding limit is enforced by the bridge. See [bot permissions](https://zulip.com/help/bots-overview).

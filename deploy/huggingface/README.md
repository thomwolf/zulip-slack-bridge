---
title: Zulip Slack Bridge
emoji: 🔗
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
suggested_hardware: cpu-upgrade
---

# Zulip–Slack bridge

Deployment preparation for the experimental [Slack–Zulip bridge](https://github.com/thomwolf/zulip-slack-bridge).

**Forwarding is not enabled in this Space.** The existing bridge continues to run locally. This container checks the bridge's offline demo and serves a setup-status page, without connecting to Slack or Zulip. It also checks Space secret presence and runs a read-only Turso connectivity query; no secret values are displayed.

Before live cutover, we need a publicly reachable Zulip server, durable transactional database storage, and a tested migration of message mappings. Standard Spaces disk is ephemeral; HF bucket mounts are not suitable for the existing SQLite WAL database. A container reporting `RUNNING` does not mean the bridge is forwarding.

No credentials or chat history are included in this repository or status page. See the GitHub repository's `docs/huggingface-spaces.md` for deployment findings and the cutover checklist.

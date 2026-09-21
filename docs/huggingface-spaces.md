# Hugging Face Spaces deployment

The deployment target is a private Docker Space in `science`. The current Docker entrypoint is **setup-only**: it runs an offline engine check and serves a status page. It does not read Slack/Zulip secrets or start forwarding. `/healthz` reports container liveness; `/readyz` returns 503 because live forwarding is not ready. Do not treat a Space marked RUNNING as a successful bridge migration.

## Findings (September 2026)

- [CPU Upgrade](https://huggingface.co/docs/hub/spaces-gpus) costs $0.03/hour (about $21.60 per 30 days), with sleeping disabled by default. Use a single replica.
- [Networking](https://huggingface.co/docs/hub/spaces-overview#networking) permits ports 80, 443, and 8080. Slack Socket Mode and an online Zulip HTTPS endpoint fit this model. The Mac's `zulip.localhost:8443` is not reachable from a Space.
- [Normal container disk](https://huggingface.co/docs/hub/spaces-storage) is ephemeral. The [legacy persistent-storage setting](https://huggingface.co/docs/hub/spaces-config-reference) is no longer available.
- HF's [bucket filesystem](https://github.com/huggingface/hf-mount#best-for--not-for) does not promise strong consistency or cross-node locking. Do not put this bridge's SQLite WAL database on it. Periodic snapshot uploads also lose acknowledged events on a crash and are not a substitute for transactional storage.

See the [durable storage comparison](durable-storage-options.md) for the recommended Turso proof of concept and alternatives.

## What remains before live operation

1. Supply a publicly reachable Zulip organization URL and a Generic bot subscribed to the chosen channel. Put its credentials in **Space Secrets**, never repository files or public Variables.
2. Configure the new [Turso libSQL backend](turso-setup.md) and run its hosted probe. The local driver and fault tests pass; real Turso validation and existing-journal migration are still required.
3. Test failure during a write, reconnect, redeploy, and worker overlap against that backend. Preserve the journal rule: record incoming events durably before acknowledging them, and record outgoing intent before calling either chat API. Do not use blind retries for uncertain writes.
4. Decide the restart policy. The current bridge requires explicit acknowledgement of possible offline gaps and has no automatic history backfill. Setting never-sleep does not prevent platform restarts.
5. Run a separate test channel pair first. Verify posts, thread promotion, edits, deletions, reactions, formatting, and images, then restart and verify existing mappings still work.
6. For migration of the same channel pair, stop the local bridge, take a consistent database backup, transfer its complete journal and mappings through a private path, and start exactly one hosted worker. If switching to a different Zulip organization, use a new bridge identity and do not reuse old mappings. Keep the local copy available for rollback, but never run both workers on the same pair.

Do not upload the live SQLite database to the public GitHub repo or a public Space/bucket: it contains message content and delivery state. The status page intentionally contains no messages, participant names, identifiers, or credentials.

## Deploy the prepared container

Upload only `Dockerfile`, `.dockerignore`, `pyproject.toml`, `uv.lock`, and `src/chat_bridge/*.py`. Use `deploy/huggingface/README.md` as the Space's root `README.md`. This explicit file list excludes the local fixture and all credentials. Configure CPU Upgrade, sleep time `-1`, one replica. Until the prerequisites above are complete, leave forwarding disabled and do not upload chat credentials.

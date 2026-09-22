"""Explicit, archived fresh starts when changing one Slack channel."""

import json
import time
import uuid
from typing import Any

from .store import Store

TABLES = ("meta", "inbox", "operations", "sends", "envelopes", "send_checks", "write_guards")


def switch_slack_channel(
    store: Store, identity: dict[str, Any], from_channel: str, *, apply: bool = False
) -> dict[str, Any]:
    """Preview or atomically archive/reset a pair; caller must hold exclusive ownership."""
    with store.lock, store.db:
        old = store.get("identity")
        if not old or old.get("slack_channel") != from_channel:
            raise ValueError("Stored Slack channel does not match --from-channel")
        changed = {k for k in old.keys() | identity.keys() if old.get(k) != identity.get(k)}
        if changed != {"slack_channel"}:
            raise ValueError("This command requires changing only the Slack channel")
        if store.db.execute("SELECT 1 FROM inbox WHERE status != 'done' LIMIT 1").fetchone():
            raise ValueError("Resolve unfinished deliveries before switching channels")
        if store.db.execute(
            "SELECT 1 FROM operations WHERE status IN ('running','uncertain','retry') LIMIT 1"
        ).fetchone():
            raise ValueError("Resolve unfinished operations before switching channels")
        snapshot = {
            table: [dict(row) for row in store.db.execute(f"SELECT * FROM {table}")]
            for table in TABLES
        }
        result = {
            "from_channel": from_channel,
            "to_channel": identity["slack_channel"],
            "mode": "fresh-start",
            "archived_rows": {table: len(rows) for table, rows in snapshot.items()},
            "applied": apply,
        }
        if not apply:
            return result
        archive = uuid.uuid4().hex
        store.db.execute(
            "INSERT INTO pair_archives(id,created,snapshot) VALUES (?,?,?)",
            (archive, time.time(), json.dumps(snapshot)),
        )
        for table in TABLES:
            store.db.execute(f"DELETE FROM {table}")
        store.db.execute("INSERT INTO meta VALUES ('identity',?)", (json.dumps(identity),))
        result["archive_id"] = archive
        return result

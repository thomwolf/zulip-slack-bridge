import json
import sqlite3

import pytest

from chat_bridge.migration import switch_slack_channel
from chat_bridge.model import Event
from chat_bridge.turso import StorageUnavailable


def identities(store):
    old = {"slack_channel": "COLD", "zulip_channel": 28, "slack_bot": "UBOT"}
    store.set("identity", old)
    store.set("state", {"messages": {"old": "private archived state"}})
    return {**old, "slack_channel": "CNEW"}


def test_switch_preview_and_atomic_archive(bridge):
    store = bridge.store
    new = identities(store)
    assert not switch_slack_channel(store, new, "COLD")["applied"]
    assert store.get("identity")["slack_channel"] == "COLD"
    result = switch_slack_channel(store, new, "COLD", apply=True)
    assert store.get("identity") == new
    assert store.get("state") is None
    archived = json.loads(
        store.db.execute(
            "SELECT snapshot FROM pair_archives WHERE id=?", (result["archive_id"],)
        ).fetchone()[0]
    )
    assert any(r["key"] == "state" for r in archived["meta"])
    with pytest.raises(ValueError, match="does not match"):
        switch_slack_channel(store, new, "COLD", apply=True)


@pytest.mark.parametrize("reason", ["pending", "uncertain", "other-change"])
def test_switch_rejects_unsafe_state(bridge, reason):
    store = bridge.store
    new = identities(store)
    if reason == "pending":
        store.ingest([Event("pending", "slack", "create", "1")])
    elif reason == "uncertain":
        with store.db:
            store.db.execute(
                "INSERT INTO operations(key,fingerprint,status) VALUES ('a','b','uncertain')"
            )
    else:
        new["zulip_channel"] = 29
    with pytest.raises(ValueError):
        switch_slack_channel(store, new, "COLD", apply=True)
    assert store.get("identity")["slack_channel"] == "COLD"
    assert store.get("state")
    assert not store.db.execute("SELECT 1 FROM pair_archives").fetchone()


def test_switch_rolls_back_archive_and_reset_together(bridge):
    store = bridge.store
    new = identities(store)
    store.db.executescript(
        "CREATE TRIGGER reject_reset BEFORE DELETE ON meta "
        "BEGIN SELECT RAISE(ABORT, 'test rollback'); END;"
    )
    with pytest.raises((sqlite3.IntegrityError, StorageUnavailable)):
        switch_slack_channel(store, new, "COLD", apply=True)
    # Turso deliberately retires the connection on any unknown database failure.
    bridge.close()
    with sqlite3.connect(bridge.config.database) as db:
        old = json.loads(db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()[0])
        assert old["slack_channel"] == "COLD"
        assert db.execute("SELECT 1 FROM meta WHERE key='state'").fetchone()
        assert not db.execute("SELECT 1 FROM pair_archives").fetchone()

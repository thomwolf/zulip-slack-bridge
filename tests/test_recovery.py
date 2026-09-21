from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chat_bridge.adapters import LiveTransport
from chat_bridge.model import DeliveryError, Event


@pytest.mark.parametrize("method", ["edit", "move", "delete"])
@pytest.mark.parametrize("outcome", ["done", "retry", "unknown"])
def test_recover_lost_mutation_response(bridge, method, outcome):
    remote = Mock()
    remote.snapshot_write.return_value = {"digest": "before"}
    remote.reconcile_write.return_value = outcome
    remote.execute.side_effect = DeliveryError("connection_lost", "uncertain")
    args = {"id": "123", "text": "new", "topic": "new"}
    with pytest.raises(DeliveryError) as error:
        bridge.store.call("event/edit", "zulip", method, args, remote)
    assert error.value.category == "retry"
    bridge.restart()
    remote.execute.side_effect = None
    remote.execute.return_value = {}
    if outcome == "unknown":
        with pytest.raises(DeliveryError) as error:
            bridge.store.call("event/edit", "zulip", method, args, remote)
        assert error.value.category == "uncertain"
    else:
        assert bridge.store.call("event/edit", "zulip", method, args, remote) == {}
    assert remote.execute.call_count == (2 if outcome == "retry" else 1)
    remote.reconcile_write.assert_called_once_with("zulip", method, args, {"digest": "before"})


def test_uncertain_upload_falls_back_without_reuploading(bridge):
    remote = Mock()
    remote.execute.side_effect = DeliveryError("lost_upload_response", "uncertain")
    args = {"attachment": {"id": "F1", "name": "image.png"}}
    with pytest.raises(DeliveryError):
        bridge.store.call("event/image", "zulip", "upload-image", args, remote)
    bridge.restart()
    result = bridge.store.call("event/image", "zulip", "upload-image", args, remote)
    assert result["skipped"] == "upload confirmation lost"
    assert remote.execute.call_count == 1


def test_unrelated_thread_continues_while_same_thread_waits(bridge):
    first = Event("first", "slack", "create", "1")
    reply = Event("reply", "slack", "create", "2", parent="1")
    unrelated = Event("unrelated", "slack", "create", "3")
    bridge.store.ingest([first, reply, unrelated])
    bridge.store.fail(first, DeliveryError("lost", "uncertain"))
    assert bridge.store.next().key == "unrelated"
    bridge.store.finish(unrelated, {})
    assert bridge.store.next() is None


def test_same_zulip_topic_waits_but_feed_messages_are_independent(bridge):
    events = [
        Event("a", "zulip", "create", "1", topic="Research"),
        Event("b", "zulip", "create", "2", topic="Research"),
        Event("c", "zulip", "create", "3", topic="Slack feed"),
    ]
    bridge.store.ingest(events)
    bridge.store.fail(events[0], DeliveryError("lost", "uncertain"))
    assert bridge.store.next().key == "c"


@pytest.mark.parametrize("platform", ["slack", "zulip"])
def test_reconcile_preserves_intervening_edit(platform):
    t = object.__new__(LiveTransport)
    t.cfg = SimpleNamespace(zulip_channel=1)
    t.snapshot_write = Mock(return_value={"digest": "third-party-edit"})
    assert (
        t.reconcile_write(platform, "edit", {"id": "1", "text": "intended"}, {"digest": "original"})
        == "unknown"
    )


def test_reconcile_confirms_applied_zulip_edit_and_delete():
    t = object.__new__(LiveTransport)
    t.cfg = SimpleNamespace(zulip_channel=1)
    t.snapshot_write = Mock(return_value={"digest": t.write_digest("intended")})
    assert t.reconcile_write("zulip", "edit", {"id": "1", "text": "intended"}, {}) == "done"
    t.snapshot_write.return_value = {"absent": True}
    assert t.reconcile_write("zulip", "delete", {"id": "1"}, {}) == "done"


def test_reconcile_read_failure_keeps_operation_uncertain(bridge):
    remote = Mock()
    remote.snapshot_write.return_value = {"digest": "before"}
    remote.execute.side_effect = DeliveryError("lost", "uncertain")
    args = {"id": "1", "text": "after"}
    with pytest.raises(DeliveryError):
        bridge.store.call("e/edit", "zulip", "edit", args, remote)
    remote.reconcile_write.side_effect = DeliveryError("read_failure", "retry")
    with pytest.raises(DeliveryError):
        bridge.store.call("e/edit", "zulip", "edit", args, remote)
    assert remote.execute.call_count == 1
    assert (
        bridge.store.db.execute("SELECT status FROM operations WHERE key='e/edit'").fetchone()[0]
        == "uncertain"
    )


def test_crash_during_reaction_recovers_without_manual_resolution(bridge):
    remote = Mock()
    remote.execute.side_effect = KeyboardInterrupt
    args = {"id": "1", "emoji": "heart", "added": True}
    with pytest.raises(KeyboardInterrupt):
        bridge.store.call("e/reaction", "zulip", "react", args, remote)
    bridge.restart()
    remote.execute.side_effect = None
    remote.execute.return_value = {}
    assert bridge.store.call("e/reaction", "zulip", "react", args, remote) == {}


def test_snapshot_read_failure_never_attempts_mutation(bridge):
    remote = Mock()
    remote.snapshot_write.side_effect = DeliveryError("read_lost", "retry")
    with pytest.raises(DeliveryError):
        bridge.store.call("e/edit", "zulip", "edit", {"id": "1", "text": "after"}, remote)
    remote.execute.assert_not_called()
    assert bridge.store.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


def test_upload_fallback_delivers_text_and_original_link(bridge):
    execute = bridge.transport.execute
    uploads = []

    def uncertain_upload(platform, method, args):
        if method == "upload-image":
            uploads.append(args)
            raise DeliveryError("lost", "uncertain")
        return execute(platform, method, args)

    bridge.transport.execute = uncertain_upload
    e = bridge.send(actor="UA", text="Photo", attachments=[{"id": "F1", "name": "a.png"}])
    with bridge.store.db:
        bridge.store.db.execute("UPDATE inbox SET next_attempt=0 WHERE key=?", (e.key,))
    assert bridge.engine.step()
    assert len(uploads) == 1
    assert bridge.store.status()["events"] == {"done": 1}
    state = bridge.store.get("state")
    message = next(iter(state["messages"].values()))
    assert "upload confirmation lost" in message["media_suffix"]
    assert "open original" in message["media_suffix"]


def test_slack_echo_receipt_recovers_lost_send(bridge):
    from slack_sdk.socket_mode.request import SocketModeRequest

    from chat_bridge.runtime import Runtime

    remote = Mock()
    remote.execute.side_effect = DeliveryError("lost", "uncertain")
    event = Event("send-event", "zulip", "create", "10")
    bridge.store.ingest([event])
    with pytest.raises(DeliveryError):
        bridge.store.call("send-event/mirror", "slack", "send", {"text": "hello"}, remote)
    bridge.store.fail(event, DeliveryError("lost", "uncertain"))
    token = bridge.store.db.execute("SELECT token FROM sends").fetchone()[0]
    runtime = SimpleNamespace(
        cfg=bridge.config,
        store=bridge.store,
        transport=SimpleNamespace(users={}),
        identity={"slack_bot": "UBOT"},
    )
    request = SocketModeRequest(
        type="events_api",
        envelope_id="receipt-envelope",
        payload={
            "team_id": "T1",
            "event_id": "echo",
            "event": {
                "type": "message",
                "channel": "C1",
                "user": "UBOT",
                "ts": "123.000001",
                "text": "hello",
                "metadata": {
                    "event_type": "zulip_slack_bridge",
                    "event_payload": {"op_key": token},
                },
            },
        },
    )
    Runtime.receive_slack(runtime, Mock(), request)
    bridge.store.reconcile_receipts()
    assert bridge.store.call("send-event/mirror", "slack", "send", {"text": "hello"}, remote) == {
        "id": "123.000001"
    }
    assert remote.execute.call_count == 1

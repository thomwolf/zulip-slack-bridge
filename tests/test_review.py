"""Regression coverage for the PRD/API review decisions."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chat_bridge.adapters import LiveTransport
from chat_bridge.model import DeliveryError
from chat_bridge.runtime import Runtime


@pytest.mark.parametrize("kind", ["edit", "delete"])
def test_late_change_posts_notice_once_and_keeps_old_copy(bridge, kind):
    bridge.send(actor="UA", author="Alice", text="Original", revision="1")
    mid = bridge.engine.find("slack", "1.000001")["zulip"]
    execute = bridge.transport.execute

    def deny_changes(platform, method, args):
        if method in {"edit", "delete"}:
            raise DeliveryError("time_limit", "denied")
        return execute(platform, method, args)

    bridge.transport.execute = deny_changes
    event = bridge.send(kind=kind, actor="UA", text="Correction", revision="2")
    bridge.store.ingest([event])
    assert not bridge.engine.step()
    assert "Original" in bridge.transport.messages[f"zulip:{mid}"]["text"]
    notices = [
        a
        for _, method, a in bridge.transport.calls
        if method == "send" and "previous content remains" in a["text"]
    ]
    assert len(notices) == 1
    assert "Correction" not in notices[0]["text"]
    assert bridge.store.status()["issues"]
    assert not bridge.store.status()["pending"]


def test_notice_failure_is_visible_and_replay_does_not_change_fallback_branch(bridge):
    bridge.send(actor="UA", text="Original", revision="1")
    execute = bridge.transport.execute

    def deny_and_fail(platform, method, args):
        if method == "edit":
            raise DeliveryError("time_limit", "denied")
        raise DeliveryError("rate_limit", "retry")

    bridge.transport.execute = deny_and_fail
    event = bridge.send(kind="edit", actor="UA", text="Correction", revision="2")
    assert bridge.store.status()["pending"]
    bridge.transport.execute = execute
    bridge.store.retry(event.key)
    assert bridge.engine.step()
    assert bridge.transport.calls[-1][1] == "send"
    assert "previous content remains" in bridge.transport.calls[-1][2]["text"]


def test_unobservable_zulip_withdrawal_removes_slack_copy(bridge):
    bridge.send("zulip", mid="20", actor="7", text="Shared", topic="Hello")
    mirror = bridge.engine.find("zulip", "20")["slack"]
    bridge.send("zulip", "delete", "20")
    assert f"slack:{mirror}" not in bridge.transport.messages
    assert bridge.store.status()["issues"][-1]["code"] == "withdrawn_deleted_or_moved_out_of_view"


def test_resolve_and_unresolve_keep_thread_without_slack_posts(bridge):
    bridge.send("zulip", mid="20", actor="7", text="Question", topic="Research")
    bridge.send("zulip", mid="21", actor="8", text="Answer", topic="Research")
    root = bridge.engine.find("zulip", "20")["root"]
    before = len(bridge.transport.calls)
    for topic in ["✔ Research", "Research"]:
        bridge.send("zulip", "move", "20", ids=["20", "21"], topic=topic)
        assert bridge.engine.topic_root(topic) == root
    assert len(bridge.transport.calls) == before + 2
    assert all(
        method == "edit" and args["id"] == root
        for _, method, args in bridge.transport.calls[before:]
    )


def test_removing_one_skin_tone_does_not_remove_another_by_same_user(bridge):
    bridge.send(actor="UA", text="Hi")
    for variant in ["+1", "+1::skin-tone-4"]:
        bridge.send(kind="reaction", actor="UB", emoji="thumbsup", reaction_variant=variant)
    bridge.send(kind="reaction", actor="UB", emoji="thumbsup", reaction_variant="+1", added=False)
    calls = [a for _, method, a in bridge.transport.calls if method == "react"]
    assert len(calls) == 1
    bridge.send(
        kind="reaction",
        actor="UB",
        emoji="thumbsup",
        reaction_variant="+1::skin-tone-4",
        added=False,
    )
    assert bridge.transport.calls[-1][2]["added"] is False


def test_positive_queue_echo_recovers_lost_response_without_resend(bridge):
    execute = bridge.transport.execute
    receipt = []

    def lost_response(platform, method, args):
        result = execute(platform, method, args)
        receipt.append((args["op_key"], platform, result["id"]))
        raise DeliveryError("lost_response", "uncertain")

    bridge.transport.execute = lost_response
    bridge.send(actor="UA", text="Only once")
    bridge.restart()
    bridge.store.ingest([], {"queue_id": "q", "last_event_id": 2}, receipts=receipt)
    bridge.transport.execute = execute
    assert bridge.engine.step()
    assert len(bridge.transport.calls) == 1
    assert bridge.store.status()["events"] == {"done": 1}


def test_wrong_platform_receipt_cannot_confirm_send(bridge):
    def uncertain(platform, method, args):
        raise DeliveryError("lost", "uncertain")

    bridge.transport.execute = uncertain
    bridge.send(actor="UA", text="Hi")
    token = bridge.store.uncertain_sends()[0]["token"]
    bridge.store.ingest([], receipts=[(token, "slack", "fake")])
    assert not bridge.engine.step()
    assert bridge.store.status()["uncertain_operations"]


def test_slack_metadata_reconciliation_requires_own_bot_and_matching_token(bridge):
    def uncertain(platform, method, args):
        raise DeliveryError("lost", "uncertain")

    execute = bridge.transport.execute
    bridge.transport.execute = uncertain
    bridge.send("zulip", mid="20", actor="7", text="Hello", topic="Research")
    token = bridge.store.uncertain_sends()[0]["token"]
    adapter = object.__new__(LiveTransport)
    adapter.cfg, adapter.slack_bot, adapter.slack = bridge.config, "UBOT", Mock()
    message = {
        "user": "OTHER",
        "ts": "2.000001",
        "metadata": {"event_type": "zulip_slack_bridge", "event_payload": {"op_key": token}},
    }
    adapter.slack.conversations_history.return_value = {"messages": [message]}
    adapter.reconcile_sends(bridge.store)
    assert not bridge.engine.step()
    message["user"] = "UBOT"
    adapter.reconcile_sends(bridge.store)
    bridge.transport.execute = execute
    assert bridge.engine.step()
    assert bridge.engine.find("zulip", "20")["root"] == "2.000001"
    assert bridge.engine.find("zulip", "20")["slack"] != "2.000001"


def test_server_limits_control_rendering_and_topic_generation(bridge):
    bridge.transport.max_message_length = 600
    bridge.transport.max_topic_length = 30
    bridge.send(actor="UA", text="x" * 2000)
    assert len(bridge.transport.calls[0][2]["text"]) <= 600
    assert "[UA](https://" in bridge.transport.calls[0][2]["text"]
    bridge.send(mid="2.000001", actor="UB", parent="1.000001", text="Reply")
    assert len(bridge.engine.state["conversations"]["1.000001"]["topic"]) <= 30


@pytest.mark.parametrize("level,requested", [(480, None), (481, 604800)])
def test_queue_timeout_is_version_gated(bridge, level, requested):
    reader = Mock()
    reader.register.return_value = {
        "result": "success",
        "queue_id": "q",
        "last_event_id": -1,
        "idle_queue_timeout_secs": requested or 600,
    }
    runtime = SimpleNamespace(
        identity={"zulip_feature_level": level},
        cfg=bridge.config,
        reader=reader,
        transport=SimpleNamespace(check_zulip=lambda x: x, zulip_bots=set()),
        store=bridge.store,
    )
    Runtime.register(runtime)
    assert (
        reader.register.call_args.kwargs["client_capabilities"]["notification_settings_null"]
        is False
    )
    assert reader.register.call_args.kwargs.get("idle_queue_timeout") == requested
    assert bridge.store.get("queue_idle_timeout_seconds") == (requested or 600)


def test_expired_queue_records_gap_and_stops_without_replacing_it(bridge):
    bridge.store.ingest([], {"queue_id": "expired", "last_event_id": 9})
    bridge.store.set("zulip_last_poll", 1234)
    reader = Mock()
    reader.call_endpoint.return_value = {"result": "error", "code": "BAD_EVENT_QUEUE_ID"}
    runtime = SimpleNamespace(store=bridge.store, reader=reader, stop=threading.Event())
    Runtime.receive_zulip(runtime)
    assert runtime.stop.is_set()
    assert bridge.store.get("zulip_gap")["from"] == 1234
    assert bridge.store.get("zulip_cursor") is None
    reader.register.assert_not_called()


def test_policy_windows_and_named_topic_block(bridge):
    adapter = object.__new__(LiveTransport)
    adapter.cfg, adapter.zulip, adapter.feature_level = bridge.config, Mock(), 500
    channel = {"stream_id": 1, "topics_policy": "inherit"}
    adapter.zulip.register.return_value = {
        "result": "success",
        "queue_id": "probe",
        "subscriptions": [channel],
        "realm_allow_message_editing": True,
        "realm_message_content_edit_limit_seconds": 600,
        "realm_message_content_delete_limit_seconds": None,
        "realm_can_move_messages_between_topics_group": 3,
        "max_topic_length": 40,
        "max_message_length": 7000,
    }
    adapter.zulip.call_endpoint.return_value = {"result": "success", "is_user_group_member": True}
    policy = adapter.read_policy(99)
    assert policy["edit_window"] == "600 seconds"
    assert policy["delete_window"] == "unlimited"
    assert next(iter(policy["permission_groups"].values()))["bot_is_member"]
    channel["topics_policy"] = "empty_topic_only"
    with pytest.raises(ValueError, match="Blocked"):
        adapter.read_policy(99)
    assert adapter.zulip.deregister.call_count == 2


def test_old_server_group_membership_is_unknown_not_assumed(bridge):
    adapter = object.__new__(LiveTransport)
    adapter.feature_level, adapter.zulip = 480, Mock()
    assert adapter.group_membership(3, 99) is None
    adapter.zulip.call_endpoint.assert_not_called()


def test_receiver_persists_own_queue_echo_with_cursor(bridge):
    bridge.send(actor="UA", text="Hello")
    token = bridge.store.db.execute("SELECT token FROM sends").fetchone()[0]
    stop = threading.Event()
    reader = Mock()
    raw = {
        "id": 1,
        "type": "message",
        "local_message_id": token,
        "message": {"id": 101, "stream_id": 1, "sender_id": 99, "type": "stream"},
    }

    def poll(*args, **kwargs):
        stop.set()
        return {"result": "success", "events": [raw]}

    reader.call_endpoint.side_effect = poll
    bridge.store.ingest([], {"queue_id": "q", "last_event_id": 0})
    runtime = SimpleNamespace(
        cfg=bridge.config,
        store=bridge.store,
        reader=reader,
        stop=stop,
        identity={"zulip_bot": "99"},
        transport=SimpleNamespace(check_zulip=lambda x: x, zulip_bots={"99"}),
    )
    Runtime.receive_zulip(runtime)
    assert bridge.store.get("zulip_cursor")["last_event_id"] == 1
    assert bridge.store.db.execute("SELECT message_id FROM sends").fetchone()[0] == "101"
    assert bridge.store.status()["events"] == {"done": 1}


def test_no_slack_metadata_match_remains_uncertain(bridge):
    def uncertain(*args):
        raise DeliveryError("lost", "uncertain")

    bridge.transport.execute = uncertain
    bridge.send("zulip", mid="20", actor="7", text="Hello", topic="Research")
    adapter = object.__new__(LiveTransport)
    adapter.cfg, adapter.slack_bot, adapter.slack = bridge.config, "UBOT", Mock()
    adapter.slack.conversations_history.return_value = {"messages": []}
    adapter.reconcile_sends(bridge.store)
    assert not bridge.engine.step()
    assert bridge.store.status()["uncertain_operations"]
    adapter.slack.chat_postMessage.assert_not_called()

import pytest

from chat_bridge.model import DeliveryError, Event


@pytest.mark.parametrize(
    "step", ["mirror", "topic-heading", "topic-copy", "feed-continuation", "topic-copy-notice"]
)
def test_all_confirmed_sends_require_ids(bridge, step):
    def lost(*args):
        raise DeliveryError("lost", "uncertain")

    bridge.transport.execute = lost
    with pytest.raises(DeliveryError):
        bridge.store.call("test/" + step, "slack", "send", {"text": "hello"}, bridge.transport)
    with pytest.raises(ValueError, match="destination message ID"):
        bridge.store.resolve("test/" + step, True)
    assert bridge.store.db.execute("SELECT status FROM operations").fetchone()[0] == "uncertain"
    bridge.store.resolve("test/" + step, True, "123.456")
    assert bridge.store.call(
        "test/" + step, "slack", "send", {"text": "hello"}, bridge.transport
    ) == {"id": "123.456"}


def test_inflight_promotion_blocks_visible_topic_reply(bridge):
    bridge.send(actor="UA", text="A conversation")
    execute = bridge.transport.execute

    def fail_pointer(platform, method, args):
        if method == "edit":
            raise DeliveryError("offline", "retry")
        return execute(platform, method, args)

    bridge.transport.execute = fail_pointer
    first = bridge.send(mid="2.000001", parent="1.000001", actor="UA", text="Reply")
    topic = next(
        v["topic"]
        for v in bridge.transport.messages.values()
        if v.get("topic") != bridge.config.feed
    )
    other = Event(
        "zulip-reply", "zulip", "create", "400", actor="7", text="From Zulip", topic=topic
    )
    bridge.store.ingest([other])
    assert not bridge.engine.step()
    bridge.transport.execute = execute
    bridge.store.retry(first.key)
    assert bridge.engine.step()
    assert bridge.engine.step()
    assert len(bridge.store.get("state")["conversations"]) == 1
    assert not bridge.store.status()["pending"]


def test_deferred_destination_reaction_replays_after_receipt(bridge):
    execute = bridge.transport.execute
    delivered = {}

    def lost(platform, method, args):
        delivered.update(execute(platform, method, args))
        raise DeliveryError("lost", "uncertain")

    bridge.transport.execute = lost
    original = bridge.send(actor="UA", text="Hello")
    # Simulate an event already deferred by a prior worker/version before binding.
    reaction = Event("reaction", "zulip", "reaction", delivered["id"], actor="7", emoji="heart")
    bridge.store.ingest([reaction])
    state = bridge.store.get(
        "state",
        {
            "messages": {},
            "index": {},
            "conversations": {},
            "tombstones": [],
            "deferred": {},
            "issues": [],
        },
    )
    state["deferred"]["zulip:" + delivered["id"]] = [reaction.json()]
    bridge.store.finish(reaction, state)
    token = bridge.store.db.execute("SELECT token FROM sends").fetchone()[0]
    bridge.store.ingest([], receipts=[(token, "zulip", delivered["id"])])
    bridge.transport.execute = execute
    assert bridge.engine.step()
    state = bridge.store.get("state")
    assert not state["deferred"]
    assert state["messages"]["slack:" + original.message_id]["reactions"]["zulip"]["heart"]


def test_delete_withdraws_full_text_correction(bridge):
    bridge.send(actor="UA", text="Original", revision="1")
    execute = bridge.transport.execute

    def denied_edit(platform, method, args):
        if method == "edit":
            raise DeliveryError("too_old", "denied")
        return execute(platform, method, args)

    bridge.transport.execute = denied_edit
    bridge.send(kind="edit", actor="UA", text="Corrected confidential text", revision="2")
    state = bridge.store.get("state")
    assert len(next(iter(state["messages"].values()))["correction_notices"]) == 1
    bridge.transport.execute = execute
    bridge.send(kind="delete", actor="UA", revision="3")
    assert not bridge.transport.messages


def test_space_retries_safe_preflight_with_backoff(monkeypatch):
    from unittest.mock import Mock

    from chat_bridge import space_app

    run = Mock(
        side_effect=[
            DeliveryError("preflight_retryable", "retry", 7),
            DeliveryError("preflight_retryable", "retry"),
            None,
        ]
    )
    sleep = Mock()
    monkeypatch.setattr(space_app, "run_pairs", run)
    monkeypatch.setattr(space_app.time, "sleep", sleep)
    space_app.run_live([])
    assert run.call_count == 3
    assert [c.args[0] for c in sleep.call_args_list] == [7, 10]


@pytest.mark.parametrize(
    "error",
    [
        ValueError("wrong config"),
        RuntimeError("ambiguous database"),
        DeliveryError("preflight_connection_or_api_failure"),
    ],
)
def test_space_does_not_retry_unsafe_startup_failure(monkeypatch, error):
    from unittest.mock import Mock

    from chat_bridge import space_app

    run = Mock(side_effect=error)
    sleep = Mock()
    monkeypatch.setattr(space_app, "run_pairs", run)
    monkeypatch.setattr(space_app.time, "sleep", sleep)
    with pytest.raises(type(error)):
        space_app.run_live([])
    assert run.call_count == 1
    sleep.assert_not_called()

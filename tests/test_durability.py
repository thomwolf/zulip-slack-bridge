import sqlite3

import pytest

from chat_bridge.model import DeliveryError


def test_confirmed_send_is_not_repeated_after_local_commit_failure(bridge, monkeypatch):
    finish = bridge.store.finish

    def crash(*args):
        raise sqlite3.OperationalError("disk unavailable")

    monkeypatch.setattr(bridge.store, "finish", crash)
    event = bridge.send(actor="UA", text="One copy")
    assert len(bridge.transport.calls) == 1
    monkeypatch.setattr(bridge.store, "finish", finish)
    bridge.restart()
    bridge.store.retry(event.key)
    assert bridge.engine.step()
    assert len(bridge.transport.calls) == 1
    assert bridge.store.status()["events"] == {"done": 1}


def test_lost_write_response_is_held_not_retried(bridge):
    def uncertain(*args):
        raise DeliveryError("connection_lost", "uncertain")

    bridge.transport.execute = uncertain
    event = bridge.send(actor="UA", text="Maybe delivered")
    bridge.restart()
    assert not bridge.engine.step()
    with pytest.raises(ValueError, match="uncertain"):
        bridge.store.retry(event.key)
    assert bridge.store.status()["uncertain_operations"]


def test_crash_during_write_is_detected_after_restart(bridge):
    def crash(*args):
        raise KeyboardInterrupt

    bridge.transport.execute = crash
    with pytest.raises(KeyboardInterrupt):
        bridge.send(actor="UA", text="Maybe delivered")
    bridge.restart()
    assert bridge.engine.step()
    assert bridge.store.status()["pending"][0]["status"] == "uncertain"


def test_rate_limit_retries_without_claiming_uncertain_delivery(bridge):
    execute = bridge.transport.execute

    def rate_limit(*args):
        raise DeliveryError("rate_limited", "retry", 1)

    bridge.transport.execute = rate_limit
    event = bridge.send(actor="UA", text="Delayed")
    assert bridge.store.status()["pending"][0]["status"] == "pending"
    bridge.transport.execute = execute
    bridge.store.retry(event.key)
    assert bridge.engine.step()
    assert len(bridge.transport.messages) == 1


def test_database_cannot_be_reused_for_different_pair(bridge):
    bridge.store.bind(bridge.config.identity())
    with pytest.raises(ValueError, match="different"):
        bridge.store.bind({**bridge.config.identity(), "slack_channel": "COTHER"})


def test_completed_inbox_drops_original_body(bridge):
    bridge.send(actor="UA", text="Do not keep full raw events forever")
    assert bridge.store.db.execute("SELECT payload FROM inbox").fetchone()[0] == "{}"


def test_operation_arguments_are_not_stored_as_plaintext(bridge):
    bridge.send(actor="UA", text="sensitive body")
    row = dict(bridge.store.db.execute("SELECT * FROM operations").fetchone())
    assert "sensitive body" not in str(row)


def test_operator_can_confirm_lost_send_without_duplicate(bridge):
    execute = bridge.transport.execute
    delivered = {}

    def lost_response(*args):
        delivered.update(execute(*args))
        raise DeliveryError("lost_response", "uncertain")

    bridge.transport.execute = lost_response
    event = bridge.send(actor="UA", text="Delivered once")
    operation = bridge.store.status()["uncertain_operations"][0]["key"]
    with pytest.raises(ValueError, match="destination message ID"):
        bridge.store.resolve(operation, True)
    bridge.store.resolve(operation, True, delivered["id"])
    bridge.store.retry(event.key)
    bridge.transport.execute = execute
    assert bridge.engine.step()
    assert len(bridge.transport.calls) == 1
    assert bridge.store.status()["events"] == {"done": 1}


def test_operator_can_confirm_no_write_and_retry(bridge):
    execute = bridge.transport.execute

    def unavailable(*args):
        raise DeliveryError("lost_connection", "uncertain")

    bridge.transport.execute = unavailable
    event = bridge.send(actor="UA", text="Not delivered yet")
    operation = bridge.store.status()["uncertain_operations"][0]["key"]
    bridge.store.resolve(operation, False)
    bridge.store.retry(event.key)
    bridge.transport.execute = execute
    assert bridge.engine.step()
    assert len(bridge.transport.calls) == 1


def test_permission_repair_allows_retry_of_rejected_send(bridge):
    execute = bridge.transport.execute

    def forbidden(*args):
        raise DeliveryError("forbidden", "denied")

    bridge.transport.execute = forbidden
    event = bridge.send(actor="UA", text="Wait for permission")
    bridge.transport.execute = execute
    bridge.store.retry(event.key)
    assert bridge.engine.step()
    assert bridge.store.status()["events"] == {"done": 1}


@pytest.mark.parametrize("method,category", [("react", "retry"), ("send", "uncertain")])
def test_lost_response_retries_only_idempotent_reactions(bridge, method, category):
    from unittest.mock import Mock

    from chat_bridge.model import DeliveryError

    transport = Mock()
    transport.execute.side_effect = DeliveryError("write_connection_lost", "uncertain")
    args = {"id": "1", "emoji": "heart", "added": True}
    with pytest.raises(DeliveryError) as raised:
        bridge.store.call("test/reaction", "zulip", method, args, transport)
    assert raised.value.category == category
    transport.execute.side_effect = None
    transport.execute.return_value = {}
    if method == "react":
        assert bridge.store.call("test/reaction", "zulip", method, args, transport) == {}
        assert transport.execute.call_count == 2
    else:
        with pytest.raises(DeliveryError):
            bridge.store.call("test/reaction", "zulip", method, args, transport)
        assert transport.execute.call_count == 1


def test_readiness_detects_held_delivery_and_retry(bridge):
    from chat_bridge.model import DeliveryError, Event

    e = Event("health-test", "slack", "reaction", "1")
    bridge.store.ingest([e])
    assert not bridge.store.delivery_blocked()
    bridge.store.fail(e, DeliveryError("timeout", "retry"))
    assert bridge.store.delivery_blocked()
    bridge.store.finish(e, {})
    assert not bridge.store.delivery_blocked()

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from slack_sdk.socket_mode.request import SocketModeRequest

from chat_bridge.runtime import Runtime


def request():
    return SocketModeRequest(
        type="events_api",
        envelope_id="envelope1",
        payload={
            "team_id": "T1",
            "event_id": "E1",
            "event": {
                "type": "message",
                "channel": "C1",
                "user": "U1",
                "ts": "1.000001",
                "text": "hello",
            },
        },
    )


def test_slack_ack_follows_durable_commit(bridge):
    runtime = SimpleNamespace(
        cfg=bridge.config, store=bridge.store, transport=SimpleNamespace(users={})
    )
    client = Mock()

    def acknowledge(response):
        assert bridge.store.status()["events"] == {"pending": 1}
        assert response.envelope_id == "envelope1"

    client.send_socket_mode_response.side_effect = acknowledge
    Runtime.receive_slack(runtime, client, request())
    client.send_socket_mode_response.assert_called_once()


def test_slack_is_not_acknowledged_if_commit_fails(bridge, monkeypatch):
    runtime = SimpleNamespace(
        cfg=bridge.config, store=bridge.store, transport=SimpleNamespace(users={})
    )
    monkeypatch.setattr(bridge.store, "ingest", Mock(side_effect=OSError("disk full")))
    client = Mock()
    with pytest.raises(OSError):
        Runtime.receive_slack(runtime, client, request())
    client.send_socket_mode_response.assert_not_called()


def test_duplicate_slack_envelope_is_acknowledged_without_second_input(bridge):
    runtime = SimpleNamespace(
        cfg=bridge.config, store=bridge.store, transport=SimpleNamespace(users={})
    )
    client = Mock()
    Runtime.receive_slack(runtime, client, request())
    Runtime.receive_slack(runtime, client, request())
    assert client.send_socket_mode_response.call_count == 2
    assert bridge.store.status()["events"] == {"pending": 1}
    assert bridge.store.db.execute("SELECT count(*) FROM envelopes").fetchone()[0] == 1

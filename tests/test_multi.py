from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import Harness
from slack_sdk.socket_mode.request import SocketModeRequest

from chat_bridge.config import load_pairs
from chat_bridge.engine import Engine
from chat_bridge.model import DeliveryError
from chat_bridge.multi import MultiRuntime
from chat_bridge.runtime import Runtime


def request(channel="CA", event="one", **fields):
    return SocketModeRequest(
        type="events_api",
        envelope_id=event,
        payload={
            "team_id": "T1",
            "event_id": event,
            "event": {
                "type": "message",
                "channel": channel,
                "ts": "1.000001",
                "user": "UA",
                "text": channel,
                **fields,
            },
        },
    )


@pytest.fixture
def pairs(tmp_path):
    harnesses = []
    runtimes = []
    for n in range(2):
        h = Harness(tmp_path / f"{n}.sqlite")
        h.config = replace(h.config, slack_channel=("CA", "CB")[n], zulip_channel=n + 1)
        h.engine = Engine(h.config, h.store, h.transport, "UBOT", "99")
        r = object.__new__(Runtime)
        r.cfg, r.store, r.transport = h.config, h.store, SimpleNamespace(users={})
        runtimes.append(r)
        harnesses.append(h)
    socket = SimpleNamespace(socket_mode_request_listeners=[])
    yield MultiRuntime(runtimes, socket), harnesses
    for h in harnesses:
        h.close()


def test_routes_identical_message_ids_and_acks_only_after_target_commit(pairs):
    multi, (a, b) = pairs
    client = Mock()
    client.send_socket_mode_response.side_effect = lambda _: (
        (a.store.status()["events"] == {"pending": 1})
        or pytest.fail("acked before durable ingestion")
    )
    multi.receive_slack(client, request())
    assert b.store.status()["events"] == {}
    client.send_socket_mode_response.side_effect = None
    multi.receive_slack(client, request("CB", "two"))
    assert a.engine.step() and b.engine.step()
    assert a.engine.state["messages"] != b.engine.state["messages"]
    assert a.transport.calls[0][2]["topic"] == "Slack feed"
    multi.receive_slack(client, request())
    assert not a.engine.step()


def test_unknown_channel_or_workspace_does_not_enter_any_queue(pairs):
    multi, hs = pairs
    client = Mock()
    multi.receive_slack(client, request("CUNKNOWN"))
    req = request()
    req.payload["team_id"] = "TOTHER"
    multi.receive_slack(client, req)
    assert client.send_socket_mode_response.call_count == 2
    assert all(h.store.status()["events"] == {} for h in hs)


def test_failed_pair_does_not_hold_other_pair(pairs):
    multi, (a, b) = pairs
    a.transport.execute = Mock(side_effect=DeliveryError("forbidden", "denied"))
    multi.receive_slack(Mock(), request())
    multi.receive_slack(Mock(), request("CB", "two"))
    assert a.engine.step() and b.engine.step()
    assert a.store.status()["events"] == {"denied": 1}
    assert b.store.status()["events"] == {"done": 1}


def test_failed_ingestion_is_not_acknowledged(pairs, monkeypatch):
    multi, (a, b) = pairs
    monkeypatch.setattr(a.store, "ingest", Mock(side_effect=OSError()))
    client = Mock()
    with pytest.raises(OSError):
        multi.receive_slack(client, request())
    client.send_socket_mode_response.assert_not_called()
    multi.receive_slack(client, request("CB", "two"))
    assert b.store.status()["events"] == {"pending": 1}


def test_reaction_routes_using_item_channel(pairs):
    multi, (a, b) = pairs
    req = request("CB", "reaction")
    req.payload["event"] = {
        "type": "reaction_added",
        "item": {"type": "message", "channel": "CB", "ts": "1.1"},
        "user": "U1",
        "reaction": "thumbsup",
    }
    multi.receive_slack(Mock(), req)
    assert a.store.next() is None
    assert b.store.next().kind == "reaction"


def test_multi_config_and_legacy(tmp_path):
    from pathlib import Path

    path = Path("bridge.multi.example.toml")
    cfg = load_pairs(path)
    assert len(cfg) == 2 and cfg[1].feed == "Slack discussions"
    assert cfg[1].turso_url_env == "TURSO_RESEARCH_DATABASE_URL"
    assert len(load_pairs(Path("bridge.example.toml"))) == 1
    for old, new in [
        ("C_RESEARCH", "C_GENERAL"),
        ("zulip_channel_id = 2", "zulip_channel_id = 1"),
        ("bridge-research.sqlite", "bridge-general.sqlite"),
        ('id = "research"', 'id = "general"'),
    ]:
        bad = tmp_path / "bad.toml"
        bad.write_text(path.read_text().replace(old, new))
        with pytest.raises(ValueError, match="unique"):
            load_pairs(bad)

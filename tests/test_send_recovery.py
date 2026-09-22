from unittest.mock import Mock

import pytest

from chat_bridge.adapters import LiveTransport
from chat_bridge.formatting import slack_mrkdwn_to_zulip, slack_rich_to_zulip
from chat_bridge.model import DeliveryError, Event


def test_two_complete_absence_checks_retry_same_receipt(bridge, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("chat_bridge.store.time.time", lambda: now[0])
    remote = Mock(supports_auto_send_recovery=True)
    remote.can_recover_send.return_value = True
    remote.execute.side_effect = DeliveryError("lost", "uncertain")
    e = Event("send", "slack", "create", "123")
    bridge.store.ingest([e])
    args = {"text": "hello", "topic": "feed"}
    with pytest.raises(DeliveryError):
        bridge.store.call("send/mirror", "zulip", "send", args, remote)
    bridge.store.fail(e, DeliveryError("lost", "uncertain"))
    token = remote.execute.call_args.args[2]["op_key"]
    now[0] = 1119
    bridge.store.confirm_send_absent("send/mirror")
    assert bridge.store.next() is None
    now[0] = 1120
    bridge.store.confirm_send_absent("send/mirror")
    now[0] = 1149
    bridge.store.confirm_send_absent("send/mirror")
    assert bridge.store.next() is None
    now[0] = 1150
    bridge.store.confirm_send_absent("send/mirror")
    assert bridge.store.next().key == "send"
    remote.execute.side_effect = None
    remote.execute.return_value = {"id": "45"}
    bridge.store.call("send/mirror", "zulip", "send", args, remote)
    assert remote.execute.call_args.args[2]["op_key"] == token


@pytest.mark.parametrize(
    "found_newest,found_oldest,marker",
    [(True, True, True), (True, True, False), (False, True, False), (True, False, False)],
)
def test_zulip_history_requires_complete_absence(found_newest, found_oldest, marker):
    t = object.__new__(LiveTransport)
    t.cfg = Mock(zulip_channel=28)
    t.zulip_bot = "763"
    t.zulip = Mock()
    t.zulip.call_endpoint.return_value = {
        "result": "success",
        "found_newest": found_newest,
        "found_oldest": found_oldest,
        "messages": (
            [
                {
                    "id": 99,
                    "sender_id": 763,
                    "timestamp": 1010,
                    "content": "[Author](https://source.test/?bridge_receipt=abc)",
                }
            ]
            if marker
            else []
        ),
    }
    store = Mock()
    t.reconcile_zulip_send(store, {"token": "abc", "started": 1000, "operation": "e/mirror"})
    assert store.ingest.called == marker
    assert store.confirm_send_absent.called == (found_newest and found_oldest and not marker)


def test_receipt_does_not_change_link_label_or_fragment():
    text = "[Alice](https://slack.test/path#message) says hello"
    tagged = LiveTransport.receipt_content(text, "abc")
    assert tagged == "[Alice](https://slack.test/path?bridge_receipt=abc#message) says hello"


def test_channel_mentions_resolve_and_code_remains_literal():
    t = object.__new__(LiveTransport)
    t.slack = Mock()
    t.slack.conversations_info.return_value = {"channel": {"name": "hf-sair-collab-zulip"}}
    blocks = [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [{"type": "channel", "channel_id": "C123"}],
                }
            ],
        }
    ]
    assert t.expand_channel_mentions(slack_rich_to_zulip(blocks)) == "#hf-sair-collab-zulip"
    assert (
        t.expand_channel_mentions(slack_mrkdwn_to_zulip("<#C123> `<#C123>`"))
        == "#hf-sair-collab-zulip `<#C123>`"
    )
    assert slack_mrkdwn_to_zulip("<#C123|channel-name>") == "#channel-name"
    t.slack.conversations_info.assert_called_once()


@pytest.mark.parametrize("case", ["legacy", "receipt"])
def test_absence_does_not_retry_legacy_or_confirmed_send(bridge, monkeypatch, case):
    now = [1000.0]
    monkeypatch.setattr("chat_bridge.store.time.time", lambda: now[0])
    remote = Mock(supports_auto_send_recovery=case != "legacy")
    remote.can_recover_send.return_value = True
    remote.execute.side_effect = DeliveryError("lost", "uncertain")
    e = Event("send", "slack", "create", "123")
    bridge.store.ingest([e])
    with pytest.raises(DeliveryError):
        bridge.store.call("send/mirror", "zulip", "send", {"text": "hello"}, remote)
    bridge.store.fail(e, DeliveryError("lost", "uncertain"))
    if case == "receipt":
        token = remote.execute.call_args.args[2]["op_key"]
        bridge.store.ingest([], receipts=[(token, "zulip", "45")])
    now[0] = 1200
    bridge.store.confirm_send_absent("send/mirror")
    now[0] = 1231
    bridge.store.confirm_send_absent("send/mirror")
    status = bridge.store.db.execute(
        "SELECT status FROM operations WHERE key='send/mirror'"
    ).fetchone()[0]
    assert status == "uncertain"
    if case == "receipt":
        bridge.store.reconcile_receipts()
        assert bridge.store.call("send/mirror", "zulip", "send", {"text": "hello"}, remote) == {
            "id": "45"
        }
        assert remote.execute.call_count == 1


def test_history_failure_never_authorizes_retry():
    t = object.__new__(LiveTransport)
    t.cfg = Mock(zulip_channel=28)
    t.zulip_bot = "763"
    t.zulip = Mock()
    t.zulip.call_endpoint.side_effect = ConnectionError()
    store = Mock()
    t.reconcile_zulip_send(store, {"token": "abc", "started": 1000, "operation": "e/mirror"})
    store.confirm_send_absent.assert_not_called()
    store.ingest.assert_not_called()

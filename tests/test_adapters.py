from unittest.mock import Mock

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.slack_response import SlackResponse

from chat_bridge.adapters import LiveTransport
from chat_bridge.content import render
from chat_bridge.model import DeliveryError


@pytest.fixture
def transport(bridge):
    adapter = object.__new__(LiveTransport)
    adapter.cfg = bridge.config
    adapter.slack = Mock()
    adapter.zulip = Mock()
    adapter.last_slack_send = 0
    adapter.zulip.call_endpoint.return_value = {"result": "success", "id": 101}
    return adapter


def test_zulip_moves_use_change_one_and_native_old_topic_notice(transport):
    transport.execute("zulip", "move", {"id": "10", "topic": "Discussion"})
    request = transport.zulip.call_endpoint.call_args.kwargs["request"]
    assert request["propagate_mode"] == "change_one"
    assert request["send_notification_to_old_thread"]
    assert not request["send_notification_to_new_thread"]


@pytest.mark.parametrize("code", ["ratelimited", "invalid_auth"])
def test_directory_retry_respects_delay_and_keeps_cursor(transport, monkeypatch, code):
    sleep = Mock()
    monkeypatch.setattr("chat_bridge.adapters.time.sleep", sleep)
    response = SlackResponse(
        client=None,
        http_verb="GET",
        api_url="test",
        req_args={},
        data={"ok": False, "error": code},
        headers={"Retry-After": "30"},
        status_code=429 if code == "ratelimited" else 200,
    )
    page = {"members": [{"id": "U1"}]}
    transport.slack.users_list.side_effect = [SlackApiError("failure", response), page]
    if code == "invalid_auth":
        with pytest.raises(SlackApiError):
            transport.slack_user_page("next-page")
        sleep.assert_not_called()
        assert transport.slack.users_list.call_count == 1
    else:
        assert transport.slack_user_page("next-page") == page
        sleep.assert_called_once_with(31)
        assert transport.slack.users_list.call_count == 2
        for call in transport.slack.users_list.call_args_list:
            assert call.kwargs == {"limit": 999, "cursor": "next-page"}


def test_zulip_send_uses_only_configured_channel(transport):
    result = transport.execute("zulip", "send", {"text": "Hi", "topic": "Feed"})
    assert result == {"id": "101"}
    assert transport.zulip.call_endpoint.call_args.kwargs["request"]["to"] == 1


def test_slack_send_is_plain_text_without_ping_parsing(transport):
    transport.slack.chat_postMessage.return_value = {"ts": "1.000001"}
    result = transport.execute("slack", "send", {"text": "<@U1> & <!channel>", "parent": ""})
    sent = transport.slack.chat_postMessage.call_args.kwargs
    assert sent["channel"] == "C1"
    assert not sent["mrkdwn"] and sent["parse"] == "none"
    assert "<@U1>" not in sent["text"]
    assert result["id"] == "1.000001"


@pytest.mark.parametrize("method", ["send", "edit"])
def test_linked_author_and_literal_body_on_slack(transport, method):
    transport.slack.chat_postMessage.return_value = {"ts": "1.000001"}
    body = "Hello <@U1> & <!channel> *literal*"
    text = render("Alice | Test", "zulip", body, "https://zulip.test/#narrow/id/31")
    transport.execute("slack", method, {"text": text, "id": "1.000001"})
    api = transport.slack.chat_postMessage if method == "send" else transport.slack.chat_update
    sent = api.call_args.kwargs
    elements = sent["blocks"][0]["elements"][0]["elements"]
    assert elements[0] == {
        "type": "link",
        "url": "https://zulip.test/#narrow/id/31",
        "text": "Alice  Test",
    }
    assert elements[1]["text"] == " · Zulip\n"
    body_elements = sent["blocks"][0]["elements"][1]["elements"]
    assert body_elements[0]["text"].startswith("Hello")
    assert all(e["type"] in {"text", "link"} for e in body_elements)
    assert elements[1]["type"] == "text"
    assert "&lt;!channel&gt;" in sent["text"]
    assert "Original:" not in sent["text"]


def test_zulip_author_links_to_slack_original(transport):
    text = render("Thom", "slack", "Hello", "https://example.slack.com/archives/C1/p1000001")
    transport.execute("zulip", "send", {"text": text, "topic": "Slack feed"})
    content = transport.zulip.call_endpoint.call_args.kwargs["request"]["content"]
    assert content == "[Thom](https://example.slack.com/archives/C1/p1000001) · Slack\nHello"


def test_zulip_permission_message_does_not_leak_content(transport):
    transport.zulip.call_endpoint.return_value = {
        "result": "error",
        "code": "BAD_REQUEST",
        "msg": "No permission: private@example.test",
    }
    with pytest.raises(DeliveryError) as raised:
        transport.execute("zulip", "edit", {"id": "1", "text": "secret"})
    assert raised.value.category == "denied"
    assert "private@" not in str(raised.value)


def test_server_error_on_write_is_uncertain(transport):
    transport.zulip.call_endpoint.return_value = {"result": "http-error", "status_code": 502}
    with pytest.raises(DeliveryError) as raised:
        transport.execute("zulip", "send", {"text": "Hi", "topic": "Feed"})
    assert raised.value.category == "uncertain"


def test_sdk_network_failure_is_uncertain_and_sanitized(transport):
    transport.slack.chat_delete.side_effect = OSError("token-secret in request")
    with pytest.raises(DeliveryError) as raised:
        transport.execute("slack", "delete", {"id": "1.000001"})
    assert raised.value.category == "uncertain"
    assert "token-secret" not in str(raised.value)


def test_rate_limit_preserves_retry_after(transport):
    response = SlackResponse(
        client=transport.slack,
        http_verb="POST",
        api_url="test",
        req_args={},
        data={"ok": False, "error": "ratelimited"},
        headers={"Retry-After": "30"},
        status_code=429,
    )
    transport.slack.chat_delete.side_effect = SlackApiError("limited", response)
    with pytest.raises(DeliveryError) as raised:
        transport.execute("slack", "delete", {"id": "1.000001"})
    assert raised.value.category == "retry" and raised.value.retry_after == 30


def test_duplicate_reaction_is_success(transport):
    transport.zulip.call_endpoint.return_value = {
        "result": "error",
        "code": "REACTION_ALREADY_EXISTS",
    }
    assert transport.execute("zulip", "react", {"id": "1", "emoji": "heart", "added": True}) == {}


def test_correlation_keys_are_attached_to_outbound_creates(transport):
    transport.queue_id = "queue"
    transport.execute("zulip", "send", {"text": "Hi", "topic": "Feed", "op_key": "token"})
    sent = transport.zulip.call_endpoint.call_args.kwargs["request"]
    assert sent["queue_id"] == "queue" and sent["local_id"] == "token"
    transport.slack.chat_postMessage.return_value = {"ts": "1.000001"}
    transport.execute("slack", "send", {"text": "Hi", "op_key": "token2"})
    sent = transport.slack.chat_postMessage.call_args.kwargs
    assert sent["metadata"]["event_payload"]["op_key"] == "token2"


def test_slack_edit_window_rejection_selects_notice_fallback(transport):
    response = SlackResponse(
        client=None,
        http_verb="POST",
        api_url="test",
        req_args={},
        data={"ok": False, "error": "edit_window_closed"},
        headers={},
        status_code=200,
    )
    transport.slack.chat_update.side_effect = SlackApiError("failure", response)
    with pytest.raises(DeliveryError) as error:
        transport.execute("slack", "edit", {"id": "1.000001", "text": "Fix"})
    assert error.value.category == "denied"

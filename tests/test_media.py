import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from chat_bridge.adapters import LiveTransport
from chat_bridge.events import slack_events, zulip_events
from chat_bridge.media import (
    MAX_IMAGE_BYTES,
    image_extension,
    read_image,
    transfer_image,
    zulip_images,
)
from chat_bridge.model import DeliveryError


def test_extract_only_organization_uploads():
    text = "[one](/user_uploads/1/a/photo.png) ![two](https://zulip.test/user_uploads/1/b/pic.jpg) [no](https://evil.test/user_uploads/1/c/a.png)"
    images = zulip_images(text, "https://zulip.test")
    assert [i["id"] for i in images] == ["/user_uploads/1/a/photo.png", "/user_uploads/1/b/pic.jpg"]


def test_slack_image_only_message_and_edit_attachments(bridge):
    raw = {
        "type": "message",
        "subtype": "file_share",
        "channel": "C1",
        "user": "UA",
        "ts": "1",
        "files": [{"id": "F1", "name": "photo.png", "url_private": "secret-url"}],
    }
    event = slack_events({"team_id": "T1", "event_id": "E1", "event": raw}, bridge.config, {})[0]
    assert event.attachments == [{"id": "F1", "name": "photo.png"}]
    assert event.text == ""
    assert "secret-url" not in str(event.json())


def test_zulip_edit_removes_attachment(bridge):
    raw = {
        "id": 1,
        "type": "update_message",
        "message_id": 7,
        "stream_id": 1,
        "content": "Removed the image",
        "user_id": 1,
    }
    assert zulip_events(raw, "q", bridge.config, set(), {"7"})[0].attachments == []


def test_bounded_download_disallows_redirects_and_checks_actual_bytes(monkeypatch):
    response = Mock(status_code=200, headers={})
    response.iter_content.return_value = [b"x" * (MAX_IMAGE_BYTES + 1)]
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    get = Mock(return_value=response)
    monkeypatch.setattr("chat_bridge.media.requests.get", get)
    with pytest.raises(DeliveryError, match="image_too_large"):
        read_image("https://files.slack.com/a", token="test-token")
    assert get.call_args.kwargs["allow_redirects"] is False
    response.status_code = 302
    with pytest.raises(DeliveryError, match="image_download_unavailable"):
        read_image("https://files.slack.com/a")


def test_slack_download_address_cannot_receive_token(monkeypatch):
    transport = SimpleNamespace(slack=Mock())
    transport.slack.files_info.return_value = {"file": {"url_private": "https://evil.test/a.png"}}
    download = Mock()
    monkeypatch.setattr("chat_bridge.media.read_image", download)
    assert transfer_image(transport, "zulip", {"id": "F1", "name": "a.png"})["skipped"]
    download.assert_not_called()


def test_slack_to_zulip_upload_verifies_signature(monkeypatch):
    transport = SimpleNamespace(slack=Mock(), zulip=Mock(), check_zulip=lambda x, **kw: x)
    transport.slack.files_info.return_value = {
        "file": {"url_private": "https://files.slack.com/a.png"}
    }
    transport.zulip.upload_file.return_value = {"url": "/user_uploads/1/a/p.png"}
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-token")
    monkeypatch.setattr(
        "chat_bridge.media.read_image", lambda *a, **kw: b"<html>Not an image</html>"
    )
    assert transfer_image(transport, "zulip", {"id": "F1", "name": "a.png"})["skipped"]
    transport.zulip.upload_file.assert_not_called()
    monkeypatch.setattr(
        "chat_bridge.media.read_image", lambda *a, **kw: b"\x89PNG\r\n\x1a\nexample"
    )
    assert transfer_image(transport, "zulip", {"id": "F1", "name": "a.png"})["url"].startswith(
        "/user_uploads/"
    )
    assert isinstance(transport.zulip.upload_file.call_args.args[0], io.BytesIO)


def test_zulip_upload_to_slack_stays_private_until_message(monkeypatch):
    transport = SimpleNamespace(
        slack=Mock(),
        zulip=Mock(),
        check_zulip=lambda x: x,
        cfg=SimpleNamespace(zulip_site="https://zulip.test"),
    )
    transport.zulip.call_endpoint.return_value = {"url": "/user_uploads/temporary/signed"}
    transport.slack.files_upload_v2.return_value = {"files": [{"id": "FNEW"}]}
    monkeypatch.setattr("chat_bridge.media.read_image", lambda *a, **kw: b"GIF89aexample")
    result = transfer_image(transport, "slack", {"id": "/user_uploads/1/a/p.gif", "name": "p.gif"})
    assert result["id"] == "FNEW" and result["name"] == "p.gif"
    assert result["uploaded_at"] > 0
    assert "channel" not in transport.slack.files_upload_v2.call_args.kwargs


def with_uploads(bridge):
    execute = bridge.transport.execute
    uploaded = []

    def transport(platform, method, args):
        if method == "upload-image":
            uploaded.append(args)
            return (
                {"url": "/user_uploads/1/copied/a.png", "name": "a.png"}
                if platform == "zulip"
                else {"id": "FCOPY", "name": "a.png"}
            )
        return execute(platform, method, args)

    bridge.transport.execute = transport
    return uploaded


def test_slack_images_survive_edits_and_promotion_without_reupload(bridge):
    uploads = with_uploads(bridge)
    bridge.send(actor="UA", text="Photo", attachments=[{"id": "F1", "name": "a.png"}])
    bridge.send(kind="edit", text="New caption", revision="2")
    assert len(uploads) == 1
    assert "/user_uploads/1/copied/a.png" in bridge.transport.calls[-1][2]["text"]
    bridge.send(mid="2.000001", text="Reply", parent="1.000001")
    parent = bridge.engine.find("slack", "1.000001")["zulip"]
    assert "/user_uploads/1/copied/a.png" in bridge.transport.messages[f"zulip:{parent}"]["text"]
    assert len(uploads) == 1
    bridge.send(kind="edit", text="New caption", revision="3", attachments=[])
    assert "/user_uploads/" not in bridge.transport.calls[-1][2]["text"]


def test_zulip_image_is_on_reply_and_removable_without_caption_change(bridge):
    uploads = with_uploads(bridge)
    markup = "[a](/user_uploads/1/a.png)"
    bridge.send(
        "zulip",
        mid="10",
        actor="1",
        topic="Photos",
        text=markup,
        attachments=[{"id": "/user_uploads/1/a.png", "name": "a.png", "markup": markup}],
    )
    assert len(uploads) == 1
    assert not bridge.transport.calls[0][2].get("images")
    assert bridge.transport.calls[1][2]["images"][0]["id"] == "FCOPY"
    bridge.send("zulip", "edit", mid="10", text="Removed", revision="2", attachments=[])
    assert bridge.transport.calls[-1][2]["images"] == []


def test_slack_image_blocks_on_send_and_edit(bridge):
    transport = object.__new__(LiveTransport)
    transport.cfg, transport.slack, transport.last_slack_send = bridge.config, Mock(), 0
    transport.slack.chat_postMessage.return_value = {"ts": "123"}
    for method in ("send", "edit"):
        transport.slack_write(
            method,
            {
                "id": "123",
                "text": "<https://zulip.test/#narrow/id/1|Alice> · Zulip\nPhoto",
                "images": [{"id": "FCOPY", "name": "photo.png"}],
            },
        )
        api = transport.slack.chat_postMessage if method == "send" else transport.slack.chat_update
        assert api.call_args.kwargs["blocks"][-1] == {
            "type": "image",
            "slack_file": {"id": "FCOPY"},
            "alt_text": "photo.png",
        }


def test_upload_uncertainty_holds_without_posting_or_reuploading(bridge):
    calls = []

    def upload(platform, method, args):
        calls.append(method)
        raise DeliveryError("upload_response_lost", "uncertain")

    bridge.transport.execute = upload
    bridge.send(actor="UA", text="Photo", attachments=[{"id": "F1", "name": "a.png"}])
    assert calls == ["upload-image"]
    bridge.restart()
    assert not bridge.engine.step()
    assert calls == ["upload-image"]
    assert bridge.store.status()["uncertain_operations"]


def test_image_signatures():
    assert image_extension(b"GIF89aexample") == "gif"
    assert image_extension(b"\xff\xd8\xffexample") == "jpg"
    assert image_extension(b"<svg/>") == ""


def test_confirmed_upload_is_reused_after_message_retry(bridge):
    uploads = with_uploads(bridge)
    execute = bridge.transport.execute

    def fail_send(platform, method, args):
        if method == "send":
            raise DeliveryError("rate_limit", "retry")
        return execute(platform, method, args)

    bridge.transport.execute = fail_send
    event = bridge.send(actor="UA", attachments=[{"id": "F1", "name": "a.png"}])
    bridge.restart()
    bridge.transport.execute = execute
    bridge.store.retry(event.key)
    assert bridge.engine.step()
    assert len(uploads) == 1
    assert not bridge.store.status()["pending"]


def test_image_limit_has_one_visible_fallback(bridge):
    uploads = with_uploads(bridge)
    bridge.send(actor="UA", attachments=[{"id": f"F{i}", "name": "a.png"} for i in range(8)])
    assert len(uploads) == 5
    assert bridge.transport.calls[-1][2]["text"].count("five-image limit") == 1


@pytest.mark.parametrize("age,category", [(0, "retry"), (180, "failed")])
def test_slack_image_processing_retries_only_recent_uploads(bridge, monkeypatch, age, category):
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.slack_response import SlackResponse

    transport = object.__new__(LiveTransport)
    transport.cfg, transport.slack, transport.last_slack_send = bridge.config, Mock(), 0
    monkeypatch.setattr("chat_bridge.adapters.time.time", lambda: 1000)
    response = SlackResponse(
        client=None,
        http_verb="POST",
        api_url="test",
        req_args={},
        data={
            "ok": False,
            "error": "invalid_blocks",
            "response_metadata": {
                "messages": [
                    "[ERROR] invalid slack file [json-pointer:/blocks/1/slack_file.id/slack_file]"
                ]
            },
        },
        headers={},
        status_code=200,
    )
    transport.slack.chat_postMessage.side_effect = SlackApiError("rejected", response)
    with pytest.raises(DeliveryError) as error:
        transport.execute(
            "slack",
            "send",
            {
                "text": "Image",
                "images": [{"id": "F1", "name": "test.png", "uploaded_at": 1000 - age}],
            },
        )
    assert error.value.category == category

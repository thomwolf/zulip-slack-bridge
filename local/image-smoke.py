"""Post a synthetic image in both test channels and verify the real transfer APIs."""

import hashlib
import io
import json
import logging
import struct
import time
import zlib
from pathlib import Path

from chat_bridge.adapters import LiveTransport
from chat_bridge.config import Config
from chat_bridge.content import render
from chat_bridge.media import transfer_image
from chat_bridge.model import DeliveryError

ROOT = Path(__file__).resolve().parent


def test_png() -> bytes:
    """Build a tiny diagnostic PNG without using any personal image data."""

    def chunk(kind: bytes, content: bytes) -> bytes:
        return (
            struct.pack("!I", len(content))
            + kind
            + content
            + struct.pack("!I", zlib.crc32(kind + content) & 0xFFFFFFFF)
        )

    width, height = 96, 64
    pixels = b"".join(
        b"\0"
        + b"".join(
            bytes((40, 165, 150) if (x // 16 + y // 16) % 2 else (235, 245, 250))
            for x in range(width)
        )
        for y in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    """Test private uploads and image blocks without simulating human accounts."""
    logging.disable(logging.CRITICAL)
    transport = LiveTransport(Config.load(ROOT / "bridge.local.toml"))
    image = io.BytesIO(test_png())
    image.name = "bridge-image-test.png"
    original = transport.check_zulip(transport.zulip.upload_file(image), mutation=True)
    upload_url = original.get("url") or original["uri"]
    zulip_source = transport.zulip_write(
        "send",
        {
            "topic": "Image transfer test",
            "text": f"Image transfer test — synthetic checkerboard.\n[image]({upload_url})",
        },
    )
    copied = transfer_image(transport, "slack", {"id": upload_url, "name": image.name})
    assert copied.get("id"), "Zulip-to-Slack upload was skipped"
    source_link = f"{transport.cfg.zulip_site}/#narrow/id/{zulip_source['id']}"
    args = {
        "text": render(
            "Image transfer test", "zulip", "Synthetic checkerboard — image test.", source_link
        ),
        "images": [copied],
    }
    for attempt in range(12):
        try:
            slack_message = transport.execute("slack", "send", args)
            break
        except DeliveryError as error:
            if error.code != "slack_image_processing" or attempt == 11:
                raise
            time.sleep(error.retry_after)
    history = transport.slack.conversations_history(
        channel=transport.cfg.slack_channel,
        oldest=slack_message["id"],
        latest=slack_message["id"],
        inclusive=True,
        limit=1,
    )
    assert any(
        b.get("slack_file", {}).get("id") == copied["id"] for b in history["messages"][0]["blocks"]
    ), "Image block missing"
    roundtrip = transfer_image(transport, "zulip", {"id": copied["id"], "name": image.name})
    assert roundtrip.get("url"), "Slack-to-Zulip upload was skipped"
    zulip_copy = transport.zulip_write(
        "send",
        {
            "topic": "Image transfer test",
            "text": f"Slack → Zulip image transfer test.\n[image]({roundtrip['url']})",
        },
    )
    message = transport.check_zulip(
        transport.zulip.call_endpoint(
            f"messages/{zulip_copy['id']}", method="GET", request={"apply_markdown": True}
        )
    )["message"]
    assert "user_uploads" in message["content"], "Zulip upload link missing"
    result = {
        "result": "passed",
        "slack_message": slack_message["id"],
        "zulip_messages": [zulip_source["id"], zulip_copy["id"]],
        "image_sha256": hashlib.sha256(image.getvalue()).hexdigest(),
        "checks": [
            "Zulip private upload",
            "Zulip-to-Slack transfer",
            "Slack image block",
            "Slack-to-Zulip transfer",
            "Zulip rendered image link",
        ],
    }
    (ROOT / "image-test-results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Image test stopped:", type(error).__name__)
        # SDK exceptions can contain authenticated payloads; report only safe codes.
        response = getattr(error, "response", None)
        if response is not None:
            print("API error:", response.get("error", "unknown"))
            print("Validation:", response.get("response_metadata", {}))
        raise SystemExit(1) from None

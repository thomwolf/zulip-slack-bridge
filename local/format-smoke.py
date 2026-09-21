"""Post a small formatting example to the two configured test channels."""

import logging
from pathlib import Path

from chat_bridge.adapters import LiveTransport
from chat_bridge.config import Config
from chat_bridge.content import render
from chat_bridge.formatting import slack_rich_to_zulip


def main() -> None:
    """Verify actual server rendering and Slack's accepted rich-text representation."""
    logging.disable(logging.CRITICAL)
    transport = LiveTransport(Config.load(Path(__file__).parent / "bridge.local.toml"))
    blocks = [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [
                        {"type": "text", "text": "Formatting test: "},
                        {"type": "text", "text": "bold ", "style": {"bold": True}},
                        {"type": "text", "text": "italic ", "style": {"italic": True}},
                        {"type": "text", "text": "strikethrough ", "style": {"strike": True}},
                        {
                            "type": "text",
                            "text": "combined ",
                            "style": {"bold": True, "italic": True, "strike": True},
                        },
                        {
                            "type": "link",
                            "text": "Open Model Initiative",
                            "url": "https://openmodel.foundation/",
                        },
                    ],
                }
            ],
        }
    ]
    markdown = slack_rich_to_zulip(blocks)
    assert markdown is not None
    preview = transport.check_zulip(
        transport.zulip.call_endpoint("messages/render", request={"content": markdown})
    )["rendered"]
    assert "<strong>" in preview and "<em>" in preview
    assert "<del>" in preview or "<s>" in preview
    assert 'href="https://openmodel.foundation/"' in preview
    zulip_message = transport.zulip_write("send", {"topic": "Formatting test", "text": markdown})
    source_link = f"{transport.cfg.zulip_site}/#narrow/id/{zulip_message['id']}"
    body = (
        markdown
        + '\n\n- First item\n- Second item\n\n> A quote\n\n```python\nprint("literal *code*")\n```'
    )
    slack_message = transport.execute(
        "slack",
        "send",
        {
            "text": render("Formatting test", "zulip", body, source_link),
        },
    )
    result = transport.slack.conversations_history(
        channel=transport.cfg.slack_channel,
        oldest=slack_message["id"],
        latest=slack_message["id"],
        inclusive=True,
        limit=1,
    )
    sections = result["messages"][0]["blocks"][0]["elements"]
    elements = [e for section in sections for e in section["elements"]]
    assert any(e.get("style", {}).get("strike") for e in elements)
    assert any(
        e.get("type") == "link" and e.get("text") == "Open Model Initiative" for e in elements
    )
    assert any(s["type"] == "rich_text_preformatted" for s in sections)
    print("Real Zulip rendering and Slack rich-text checks passed.")
    print("Zulip message:", zulip_message["id"], "Slack message:", slack_message["id"])


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Formatting test stopped:", type(error).__name__)
        response = getattr(error, "response", None)
        if response is not None:
            print("API error:", response.get("error", "unknown"))
        raise SystemExit(1) from None

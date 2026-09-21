import pytest

from chat_bridge.content import render, slack_message
from chat_bridge.events import slack_events
from chat_bridge.formatting import (
    MARKDOWN,
    slack_mrkdwn_to_zulip,
    slack_rich_to_zulip,
    zulip_to_slack_sections,
)


def flatten(sections):
    return [element for section in sections for element in section["elements"]]


def test_zulip_named_links_and_nested_styles_become_slack_elements():
    body = (
        "We are [Open Model Initiative](https://openmodel.foundation/). "
        "**Bold**, *italic*, ~~strike~~ and ***~~all three~~***."
    )
    elements = flatten(zulip_to_slack_sections(body, "https://zulip.test"))
    link = next(e for e in elements if e["type"] == "link")
    assert link == {
        "type": "link",
        "url": "https://openmodel.foundation/",
        "text": "Open Model Initiative",
    }
    assert next(e for e in elements if e["text"] == "all three")["style"] == {
        "bold": True,
        "italic": True,
        "strike": True,
    }
    assert not any("](" in e["text"] for e in elements)


def test_slack_structured_styles_and_links_render_correct_zulip_html():
    blocks = [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [
                        {"type": "text", "text": "Hey "},
                        {"type": "text", "text": "just ", "style": {"italic": True}},
                        {"type": "text", "text": "sent ", "style": {"bold": True}},
                        {"type": "text", "text": "email ", "style": {"strike": True}},
                        {
                            "type": "text",
                            "text": "to the collab",
                            "style": {"italic": True, "bold": True, "strike": True},
                        },
                        {"type": "text", "text": "\nWe are the "},
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
    converted = slack_rich_to_zulip(blocks)
    rendered = MARKDOWN.render(converted)
    assert "<em>just</em>" in rendered
    assert "<strong>sent</strong>" in rendered
    assert "<s>email</s>" in rendered
    assert "<s><em><strong>to the collab</strong></em></s>" in rendered
    assert '<a href="https://openmodel.foundation/">Open Model Initiative</a>' in rendered


@pytest.mark.parametrize(
    "source,expected",
    [
        ("*bold* _italic_ ~strike~", "**bold** *italic* ~~strike~~"),
        ("~_*combined*_~", "~~***combined***~~"),
        ("snake_case and `*literal*`", "snake_case and `*literal*`"),
        (
            "<https://example.com/path?a=1&amp;b=2|a link>",
            "[a link](https://example.com/path?a=1&b=2)",
        ),
    ],
)
def test_slack_fallback_markdown(source, expected):
    assert slack_mrkdwn_to_zulip(source) == expected


def test_code_links_and_mentions_stay_literal_in_code():
    body = "`[x](https://example.com) ~~strike~~`\n\n```python\n<@U1> **bold**\n```"
    sections = zulip_to_slack_sections(body, "https://zulip.test")
    assert sections[0]["elements"][0]["style"] == {"code": True}
    assert sections[0]["elements"][0]["type"] == "text"
    assert sections[1]["type"] == "rich_text_preformatted"
    assert sections[1]["elements"][0]["text"] == "<@U1> **bold**"


def test_lists_quotes_and_unsafe_links():
    sections = zulip_to_slack_sections(
        "- One\n- Two\n\n> Quote\n\n[unsafe](javascript:alert) <@U1> <!channel>",
        "https://zulip.test",
    )
    assert sections[0]["elements"][0]["text"] == "• "
    assert sections[2]["type"] == "rich_text_quote"
    assert all(e["type"] in {"text", "link"} for e in flatten(sections))
    assert not any(e.get("url", "").startswith("javascript:") for e in flatten(sections))


def test_rich_slack_message_normalization_and_edit_echo_guard(bridge):
    rich = [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [{"type": "text", "text": "bold", "style": {"bold": True}}],
                }
            ],
        }
    ]
    raw = {
        "type": "message",
        "channel": "C1",
        "user": "UA",
        "ts": "1",
        "text": "*bold*",
        "blocks": rich,
    }
    event = slack_events({"team_id": "T1", "event_id": "E1", "event": raw}, bridge.config, {})[0]
    assert event.text == "**bold**" and event.text_format == "zulip"
    assert render(
        "Alice", "slack", event.text, "https://slack.test", text_format=event.text_format
    ).endswith("**bold**")
    raw["bot_id"] = "B1"
    changed = {"type": "message", "subtype": "message_changed", "channel": "C1", "message": raw}
    echo = slack_events({"team_id": "T1", "event_id": "E2", "event": changed}, bridge.config, {})[0]
    assert echo.text == "*bold*" and echo.text_format == ""


def test_linked_attribution_and_api_fallback_preserve_echo_content():
    text = render(
        "Alice",
        "zulip",
        "[Link](https://example.com) ~~removed~~",
        "https://zulip.test/#narrow/id/1",
    )
    payload = slack_message(text)
    assert payload["text"] == text
    elements = flatten(payload["blocks"][0]["elements"])
    assert sum(e["type"] == "link" for e in elements) == 2
    assert next(e for e in elements if e["text"] == "removed")["style"]["strike"]


def test_bare_urls_mail_links_and_code_keep_their_content():
    elements = flatten(
        zulip_to_slack_sections(
            "See https://example.com/a. [Email](mailto:team@example.com)", "https://zulip.test"
        )
    )
    assert [e["url"] for e in elements if e["type"] == "link"] == [
        "https://example.com/a",
        "mailto:team@example.com",
    ]
    result = render(
        "Alice", "slack", "<mailto:team@example.com|Email> `user@example.com`", "https://slack.test"
    )
    assert "[Email](mailto:team@example.com)" in result
    assert "`user@example.com`" in result

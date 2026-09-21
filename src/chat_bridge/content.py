"""Conservative formatting and a deliberately small standard emoji vocabulary."""

import hashlib
import html
import re
from urllib.parse import quote

from .formatting import slack_mrkdwn_to_zulip, zulip_to_slack_sections

EMOJI = {
    "thumbsup": "1f44d",
    "thumbsdown": "1f44e",
    "heart": "2764",
    "eyes": "1f440",
    "tada": "1f389",
    "smile": "1f604",
    "laughing": "1f606",
    "thinking": "1f914",
    "clap": "1f44f",
    "fire": "1f525",
    "rocket": "1f680",
    "white_check_mark": "2705",
}
ALIASES = {
    "+1": "thumbsup",
    "-1": "thumbsdown",
    "thinking_face": "thinking",
    "check": "white_check_mark",
}
ZULIP_NAMES = {
    "thumbsup": "thumbs_up",
    "thumbsdown": "thumbs_down",
    "tada": "tada",
    "thinking": "thinking",
    "white_check_mark": "check",
}


def emoji_name(name: str, code: str = "", reaction_type: str = "unicode_emoji") -> str:
    """Normalize only tested standard emoji; custom emoji are deliberately unsupported."""
    if reaction_type != "unicode_emoji":
        return ""
    if code:
        return next((n for n, c in EMOJI.items() if c == code), "")
    name = re.sub(r"::skin-tone-[2-6]$", "", name)
    name = ALIASES.get(name, name)
    return name if name in EMOJI else ""


def neutral(text: str) -> str:
    """Disable Zulip mention syntax without pretending to resolve identities."""
    return re.sub(
        r"```[\s\S]*?```|`[^`\n]*`|(?:https?://|mailto:)[^\s)>]+|@",
        lambda match: "@\u200b" if match[0] == "@" else match[0],
        text,
    )


def slack_to_text(text: str) -> str:
    """Decode Slack links and render mentions as inert readable labels."""
    text = re.sub(r"<@[A-Z0-9]+>", "@Slack user", text)
    text = re.sub(r"<![^>]+>", "@group", text)
    text = re.sub(r"<#([A-Z0-9]+)(?:\|([^>]+))?>", lambda m: f"#{m[2] or m[1]}", text)
    text = re.sub(
        r"<(https?://[^>|]+)(?:\|([^>]+))?>", lambda m: f"{m[2]} ({m[1]})" if m[2] else m[1], text
    )
    return neutral(html.unescape(text))


def label(text: str) -> str:
    """Prevent an untrusted display name from introducing formatting or extra lines."""
    return re.sub(r"[\[\]<>*_`\\\r\n]", "", neutral(text))[:100]


def render(
    author: str,
    platform: str,
    text: str,
    link: str,
    topic: str = "",
    limit: int = 9000,
    destination: str | None = None,
    text_format: str = "",
) -> str:
    """Render a readable body with attribution, safe mentions and a bounded length."""
    body = slack_mrkdwn_to_zulip(text) if platform == "slack" and text_format != "zulip" else text
    if destination == "zulip" or platform == "slack":
        body = neutral(body)
    name = label(author).replace("|", "") or "Unknown author"
    url = quote(link, safe=":/#?=&%")
    destination = destination or ("zulip" if platform == "slack" else "slack")
    attribution = f"[{name}]({url})" if destination == "zulip" else f"<{url}|{name}>"
    prefix = f"{attribution} · {platform.title()}"
    if topic:
        prefix += f" · {label(topic)}"
    budget = max(0, limit - len(prefix) - 1)
    if len(body) > budget:
        marker = "\n[Truncated — read the original]"
        body = f"{body[: max(0, budget - len(marker))]}{marker}"
    return f"{prefix}\n{body}"


def slack_message(text: str) -> dict:
    """Link the generated author attribution; keep source content as literal text."""
    match = re.match(r"^<(https?://[^<>|\s]+)\|([^<>|\n]+)>( · Zulip[^\n]*\n)", text)
    if not match:
        return {"text": html.escape(text, quote=False), "mrkdwn": False, "blocks": []}
    url, name, _ = match.groups()
    remainder = text[match.end(2) + 1 :]
    attribution, _, body = remainder.partition("\n")
    # Keep the API fallback unchanged for durable echo matching; the visible blocks
    # express formatting explicitly and cannot generate mentions from source text.
    fallback = f"<{html.escape(url, quote=False)}|{html.escape(name, quote=False)}>"
    sections = [
        {
            "type": "rich_text_section",
            "elements": [
                {"type": "link", "url": url, "text": name},
                {"type": "text", "text": attribution + "\n"},
            ],
        }
    ]
    sections.extend(zulip_to_slack_sections(body, url))
    return {
        "text": fallback + html.escape(remainder, quote=False),
        "mrkdwn": True,
        "blocks": [{"type": "rich_text", "elements": sections}],
    }


def topic_name(text: str, parent: str, limit: int = 60) -> str:
    """Generate a stable collision-resistant title under the conservative 60-char limit."""
    title = label(slack_to_text(text)).strip() or "Discussion"
    suffix = hashlib.sha256(parent.encode()).hexdigest()[:10]
    return f"{title[: max(1, limit - 13)]} [{suffix}]"

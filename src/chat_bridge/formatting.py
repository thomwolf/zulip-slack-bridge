"""Translate shared formatting without turning source text into destination mentions."""

import html
import re
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

from markdown_it import MarkdownIt

MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("strikethrough")


def markdown_text(text: str) -> str:
    """Escape literal Slack text before adding explicit Markdown styles."""
    return re.sub(r"([\\`*_{}\[\]<>~])", r"\\\1", text)


def safe_url(url: str, base: str = "") -> str:
    """Permit ordinary web/mail links, never executable or credential-bearing links."""
    url = urljoin(base, url)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https", "mailto"} or parsed.username or parsed.password:
        return ""
    return quote(url, safe=":/#?=&%+@,;~!$")


def code_span(text: str) -> str:
    """Keep embedded backticks literal in inline code."""
    fence = "`" * (max([len(m[0]) for m in re.finditer(r"`+", text)] or [0]) + 1)
    return f"{fence} {text} {fence}" if "`" in text else f"{fence}{text}{fence}"


def styled(text: str, style: dict[str, Any]) -> str:
    """Nest supported styles with whitespace outside the Markdown delimiters."""
    if not text.strip():
        return text
    left = text[: len(text) - len(text.lstrip())]
    right = text[len(text.rstrip()) :]
    body = text.strip()
    if style.get("code"):
        body = code_span(body)
    else:
        body = markdown_text(body)
    for key, marker in [("italic", "*"), ("bold", "**"), ("strike", "~~")]:
        if style.get(key):
            body = marker + body + marker
    return left + body + right


def slack_user_name(
    user_id: str, users: dict[str, dict[str, Any]] | None = None, fallback: str = ""
) -> str:
    """Use Slack display names as literal text without mapping destination accounts."""
    user = (users or {}).get(user_id, {})
    profile = user.get("profile", {})
    name = (
        profile.get("display_name")
        or profile.get("real_name")
        or user.get("real_name")
        or user.get("name")
        or fallback
        or user_id
        or "Slack user"
    )
    return " ".join(str(name).split())


def slack_rich_to_zulip(
    blocks: list[dict[str, Any]], users: dict[str, dict[str, Any]] | None = None
) -> str | None:
    """Read Slack's structured styles, retaining links, lists, quotes and code."""
    rich = [b for b in blocks if b.get("type") == "rich_text"]
    if not rich:
        return None

    def inline(elements: list[dict[str, Any]]) -> str:
        result = ""
        for element in elements:
            kind = element.get("type")
            if kind == "text":
                result += styled(element.get("text", ""), element.get("style", {}))
            elif kind == "link":
                url = safe_url(element.get("url", ""))
                name = element.get("text") or element.get("url", "")
                caption = styled(name, element.get("style", {}))
                result += f"[{caption}]({url})" if url else markdown_text(name)
            elif kind == "emoji":
                result += ":" + element.get("name", "emoji") + ":"
            elif kind == "user":
                result += "@" + styled(
                    slack_user_name(element.get("user_id", ""), users), element.get("style", {})
                )
            elif kind == "channel":
                result += "#Slack channel"
            elif kind in {"broadcast", "usergroup"}:
                result += "@group"
            else:
                result += markdown_text(element.get("text", "[Rich content — open original]"))
        return result

    parts = []
    for block in rich:
        for section in block.get("elements", []):
            kind = section.get("type")
            elements = section.get("elements", [])
            if kind == "rich_text_preformatted":
                raw = "".join(e.get("text", "") for e in elements)
                fence = "`" * max(3, max([len(m[0]) + 1 for m in re.finditer(r"`+", raw)] or [3]))
                parts.append(f"{fence}\n{raw}\n{fence}")
            elif kind == "rich_text_list":
                indent = "  " * min(int(section.get("indent", 0)), 8)
                start = int(section.get("offset", 0)) + 1
                parts.append(
                    "\n".join(
                        indent
                        + (f"{i}. " if section.get("style") == "ordered" else "- ")
                        + inline(item.get("elements", []))
                        for i, item in enumerate(elements, start)
                    )
                )
            elif kind == "rich_text_quote":
                parts.append("\n".join("> " + line for line in inline(elements).splitlines()))
            else:
                parts.append(inline(elements))
    return "\n\n".join(parts)


def slack_mrkdwn_to_zulip(text: str, users: dict[str, dict[str, Any]] | None = None) -> str:
    """Convert Slack fallback text while protecting code, links and literal identifiers."""
    text = text.replace("\x00", "")
    tokens: list[str] = []

    def keep(value: str) -> str:
        tokens.append(value)
        return f"\x00{len(tokens) - 1}\x00"

    text = re.sub(r"```[\s\S]*?```|`[^`\n]+`", lambda m: keep(m[0]), text)
    text = re.sub(
        r"<(https?://[^>|]+|mailto:[^>|]+)(?:\|([^>]+))?>",
        lambda m: keep(
            f"[{markdown_text(html.unescape(m[2] or m[1]))}]({safe_url(html.unescape(m[1]))})"
        ),
        text,
    )
    text = re.sub(
        r"<@([A-Z0-9]+)(?:\|([^>]+))?>",
        lambda m: keep("@" + markdown_text(slack_user_name(m[1], users, m[2] or ""))),
        text,
    )
    text = re.sub(r"<![^>]+>", "@group", text)
    text = re.sub(r"<#([A-Z0-9]+)(?:\|([^>]+))?>", lambda m: "#" + (m[2] or m[1]), text)
    # Placeholders prevent one dialect's output delimiters being converted twice.
    for source, destination in [("*", "**"), ("_", "*"), ("~", "~~")]:
        pattern = rf"(?<![^\W_]){re.escape(source)}(?=\S)(.+?)(?<=\S){re.escape(source)}(?![^\W_])"
        text = re.sub(
            pattern, lambda m, marker=destination: keep(marker) + m[1] + keep(marker), text
        )
    for _ in range(len(tokens) + 1):
        converted = re.sub(r"\x00(\d+)\x00", lambda m: tokens[int(m[1])], text)
        if converted == text:
            break
        text = converted
    return html.unescape(text)


def zulip_to_slack_sections(text: str, base: str) -> list[dict[str, Any]]:
    """Convert Markdown tokens to Slack rich-text elements, never Slack mention objects."""
    sections = []
    list_stack: list[dict[str, Any]] = []
    item_start = False
    quote_depth = 0
    for token in MARKDOWN.parse(text):
        kind = token.type
        if kind in {"bullet_list_open", "ordered_list_open"}:
            list_stack.append(
                {"ordered": kind == "ordered_list_open", "number": token.attrGet("start") or 1}
            )
        elif kind in {"bullet_list_close", "ordered_list_close"}:
            list_stack.pop()
        elif kind == "list_item_open":
            item_start = True
        elif kind == "blockquote_open":
            quote_depth += 1
        elif kind == "blockquote_close":
            quote_depth -= 1
        elif kind in {"fence", "code_block"}:
            sections.append(
                {
                    "type": "rich_text_preformatted",
                    "elements": [{"type": "text", "text": token.content.rstrip("\n") or " "}],
                }
            )
        elif kind == "inline":
            elements: list[dict[str, Any]] = []
            styles: dict[str, bool] = {}
            links: list[str] = []
            if item_start and list_stack:
                current = list_stack[-1]
                prefix = f"{current['number']}. " if current["ordered"] else "• "
                current["number"] += 1
                elements.append({"type": "text", "text": "  " * (len(list_stack) - 1) + prefix})
                item_start = False
            for child in token.children or []:
                if child.type in {"strong_open", "em_open", "s_open"}:
                    styles[
                        {"strong_open": "bold", "em_open": "italic", "s_open": "strike"}[child.type]
                    ] = True
                elif child.type in {"strong_close", "em_close", "s_close"}:
                    styles.pop(
                        {"strong_close": "bold", "em_close": "italic", "s_close": "strike"}[
                            child.type
                        ],
                        None,
                    )
                elif child.type == "link_open":
                    links.append(safe_url(str(child.attrGet("href") or ""), base))
                elif child.type == "link_close":
                    links.pop()
                elif child.type in {
                    "text",
                    "code_inline",
                    "softbreak",
                    "hardbreak",
                    "html_inline",
                    "image",
                }:
                    value = "\n" if child.type in {"softbreak", "hardbreak"} else child.content
                    if not value:
                        continue
                    style = {**styles, **({"code": True} if child.type == "code_inline" else {})}
                    element: dict[str, Any] = {"type": "text", "text": value}
                    if links and links[-1]:
                        element.update(type="link", url=links[-1])
                    if style:
                        element["style"] = style
                    if child.type == "text" and not links:
                        offset = 0
                        for match in re.finditer(r"https?://[^\s<>]+", value):
                            candidate = match[0].rstrip(".,;:!?")
                            while candidate.endswith(")") and candidate.count(
                                ")"
                            ) > candidate.count("("):
                                candidate = candidate[:-1]
                            url = safe_url(candidate)
                            if not url:
                                continue
                            if match.start() > offset:
                                elements.append({**element, "text": value[offset : match.start()]})
                            elements.append(
                                {**element, "type": "link", "url": url, "text": candidate}
                            )
                            offset = match.start() + len(candidate)
                        if offset < len(value):
                            elements.append({**element, "text": value[offset:]})
                    else:
                        elements.append(element)
            if elements:
                sections.append(
                    {
                        "type": "rich_text_quote" if quote_depth else "rich_text_section",
                        "elements": elements,
                    }
                )
        elif kind == "hr":
            sections.append(
                {"type": "rich_text_section", "elements": [{"type": "text", "text": "────────"}]}
            )
    return sections

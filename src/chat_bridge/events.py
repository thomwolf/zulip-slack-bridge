"""Translate platform payloads without performing network requests in callbacks."""

from typing import Any

from .config import Config
from .content import emoji_name
from .formatting import slack_rich_to_zulip
from .media import zulip_images
from .model import Event


def slack_events(
    payload: dict[str, Any], cfg: Config, users: dict[str, dict[str, Any]]
) -> list[Event]:
    """Filter workspace/channel boundaries and normalize Slack event envelopes."""
    if payload.get("team_id") != cfg.slack_team or not payload.get("event_id"):
        return []
    raw = payload.get("event", {})
    item = raw.get("item", {})
    if raw.get("channel", item.get("channel")) != cfg.slack_channel:
        return []
    key = f"slack:{payload['event_id']}"
    kind = raw.get("type")
    if kind in {"reaction_added", "reaction_removed"}:
        if item.get("type") != "message":
            return []
        emoji = emoji_name(raw["reaction"])
        if not emoji:
            return []
        return [
            Event(
                key,
                "slack",
                "reaction",
                str(item["ts"]),
                actor=raw["user"],
                emoji=emoji,
                reaction_variant=raw["reaction"],
                added=kind == "reaction_added",
            )
        ]
    if kind != "message":
        return []
    subtype = raw.get("subtype")
    if subtype == "message_deleted":
        return [Event(key, "slack", "delete", str(raw["deleted_ts"]))]
    msg = raw.get("message", {}) if subtype == "message_changed" else raw
    if subtype != "message_changed" and subtype not in {None, "thread_broadcast", "file_share"}:
        return []
    user = str(msg.get("user", ""))
    profile = users.get(user, {})
    if subtype != "message_changed" and (msg.get("bot_id") or profile.get("is_bot") or not user):
        return []
    author = profile.get("profile", {}).get("display_name") or profile.get("real_name") or user
    text = msg.get("text", "")
    text_format = ""
    if not msg.get("bot_id") and not profile.get("is_bot"):
        formatted = slack_rich_to_zulip(msg.get("blocks", []))
        if formatted is not None:
            text, text_format = formatted, "zulip"
    if msg.get("attachments"):
        text += "\n[Attachment on Slack — see original]"
    if not text and msg.get("blocks"):
        text = "[Rich content on Slack — see original]"
    edited = msg.get("edited", {})
    return [
        Event(
            key,
            "slack",
            "edit" if subtype == "message_changed" else "create",
            str(msg["ts"]),
            actor=str(edited.get("user", user)),
            author=author,
            text=text,
            text_format=text_format,
            parent=str(msg.get("thread_ts", "")),
            revision=str(edited.get("ts", msg["ts"])),
            attachments=[
                {"id": f["id"], "name": f.get("name", "image")}
                for f in msg.get("files", [])
                if f.get("id")
            ]
            if "files" in msg
            else None,
        )
    ]


def zulip_events(
    raw: dict[str, Any], queue: str, cfg: Config, bot_ids: set[str], known_ids: set[str]
) -> list[Event]:
    """Normalize Zulip events, including multi-message moves and deletions."""
    key = f"zulip:{queue}:{raw['id']}"
    kind = raw.get("type")
    actor = str(raw.get("user_id", ""))
    if kind == "message":
        msg = raw["message"]
        if (
            msg.get("type") not in {"stream", "channel"}
            or msg.get("stream_id") != cfg.zulip_channel
        ):
            return []
        actor = str(msg["sender_id"])
        if actor in bot_ids:
            return []
        text = msg["content"]
        return [
            Event(
                key,
                "zulip",
                "create",
                str(msg["id"]),
                actor=actor,
                author=msg["sender_full_name"],
                text=text,
                topic=msg["subject"],
                revision=str(msg.get("timestamp", 0)),
                attachments=zulip_images(text, cfg.zulip_site),
            )
        ]
    ids = [str(mid) for mid in raw.get("message_ids", [raw.get("message_id", "")])]
    in_scope = raw.get("stream_id") == cfg.zulip_channel
    eligible = [mid for mid in ids if in_scope or mid in known_ids]
    if kind == "delete_message":
        return [Event(f"{key}:{mid}", "zulip", "delete", mid) for mid in eligible]
    if kind == "reaction":
        mid = str(raw["message_id"])
        emoji = emoji_name(
            raw["emoji_name"], raw.get("emoji_code", ""), raw.get("reaction_type", "unicode_emoji")
        )
        if mid not in known_ids or actor in bot_ids or not emoji:
            return []
        return [
            Event(key, "zulip", "reaction", mid, actor=actor, emoji=emoji, added=raw["op"] == "add")
        ]
    if kind != "update_message" or not eligible:
        return []
    result = []
    if "subject" in raw or "new_stream_id" in raw:
        destination = raw.get("new_stream_id", raw.get("stream_id", cfg.zulip_channel))
        result.append(
            Event(
                f"{key}:move",
                "zulip",
                "move",
                eligible[0],
                actor=actor,
                topic=raw.get("subject", ""),
                ids=eligible,
                out_of_scope=destination != cfg.zulip_channel,
            )
        )
    if "content" in raw and not raw.get("rendering_only") and actor != "None":
        mid = str(raw["message_id"])
        if mid in eligible:
            result.append(
                Event(
                    f"{key}:edit",
                    "zulip",
                    "edit",
                    mid,
                    actor=actor,
                    text=raw["content"],
                    revision=str(raw.get("edit_timestamp", 0)),
                    attachments=zulip_images(raw["content"], cfg.zulip_site),
                )
            )
    return result


def zulip_bot_ids(snapshot: dict[str, Any]) -> set[str]:
    """Include cross-realm system bots, which the ordinary users endpoint omits."""
    return {
        str(user["user_id"])
        for field in ("realm_users", "cross_realm_bots", "realm_non_active_users")
        for user in snapshot.get(field, [])
        if user.get("is_bot")
    }

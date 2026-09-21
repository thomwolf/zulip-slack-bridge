"""Conversation rules, independent of the Slack and Zulip SDKs."""

import hashlib
import html
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from .config import Config
from .content import label, render, topic_name
from .media import MAX_IMAGES
from .model import DeliveryError, Event, Platform, Transport
from .store import Store


def initial_state() -> dict[str, Any]:
    """Return a JSON-compatible routing snapshot."""
    return {
        "messages": {},
        "index": {},
        "conversations": {},
        "tombstones": [],
        "deferred": {},
        "issues": [],
    }


class Engine:
    """Process one input at a time, journaling every external mutation before sending."""

    def __init__(
        self, config: Config, store: Store, transport: Transport, slack_bot: str, zulip_bot: str
    ) -> None:
        self.config, self.store, self.transport = config, store, transport
        self.bots = {"slack": slack_bot, "zulip": zulip_bot}
        self.state = initial_state()

    def step(self) -> bool:
        """Deliver the oldest ready event. Failed work holds the FIFO for operator repair."""
        self.store.reconcile_receipts()
        event = self.store.next(self.config.feed)
        if event is None:
            return False
        self.state = self.store.get("state", initial_state())
        try:
            self.apply(event)
            self.store.finish(event, self.state)
        except DeliveryError as error:
            self.store.fail(event, error)
        except Exception:
            # Operation journal retains any ambiguous remote writes; don't log message bodies.
            self.store.fail(event, DeliveryError("unexpected_event_failure"))
        return True

    def call(
        self, e: Event, step: str, platform: Platform, method: str, **args: Any
    ) -> dict[str, Any]:
        """Execute/replay a deterministic operation belonging to this event."""
        return self.store.call(f"{e.key}/{step}", platform, method, args, self.transport)

    def find(self, platform: str, message_id: str) -> dict[str, Any] | None:
        """Look up either copy of a message without conflating author and reactor."""
        key = self.state["index"].get(f"{platform}:{message_id}")
        return self.state["messages"].get(key) if key else None

    def link(self, platform: str, message_id: str) -> str:
        """Construct message-identity links; Zulip links survive topic moves."""
        if platform == "slack":
            site = getattr(self.transport, "slack_site", "https://example.slack.com")
            return f"{site}/archives/{self.config.slack_channel}/p{message_id.replace('.', '')}"
        return f"{self.config.zulip_site}/#narrow/id/{message_id}"

    def issue(self, code: str, message_id: str) -> None:
        """Keep a bounded, payload-free list of non-delivery problems."""
        entry = {"code": code, "message_id": message_id}
        if entry not in self.state["issues"]:
            self.state["issues"].append(entry)
            self.state["issues"] = self.state["issues"][-100:]

    def topic_heading(self, topic: str) -> str:
        """Render a linked Slack heading using Zulip's topic URL encoding."""
        encoded = quote(topic, safe="").replace(".", ".2E").replace("%", ".")
        url = (
            f"{self.config.zulip_site}/#narrow/channel/{self.config.zulip_channel}/topic/{encoded}"
        )
        title = label(topic).replace("|", "") or "General chat"
        return f"<{url}|{title}> · Zulip topic\n"

    def apply(self, e: Event) -> None:
        """Apply a normalized input to an in-memory snapshot before atomic commit."""
        if (
            e.platform == "zulip"
            and e.kind != "reaction"
            and e.message_id in self.state.get("feed_pointers", [])
        ):
            return
        if e.kind == "move":
            self.move(e)
            return
        if e.kind in {"create", "reaction"} and e.actor == self.bots[e.platform]:
            return
        heading = self.state["conversations"].get(e.message_id) if e.platform == "slack" else None
        if heading and heading.get("heading"):
            if e.kind == "delete" or (
                e.kind == "edit"
                and html.unescape(e.text).strip()
                not in {h.strip() for h in heading.get("heading_echoes", [heading["heading"]])}
            ):
                heading["blocked"] = True
                self.issue("topic_heading_changed_locally", e.message_id)
            return
        message = self.find(e.platform, e.message_id)
        if e.kind == "create":
            if message is None:
                self.create(e)
            return
        if message is None:
            key = f"{e.platform}:{e.message_id}"
            if e.kind == "delete":
                if key not in self.state["tombstones"]:
                    self.state["tombstones"].append(key)
                self.state["deferred"].pop(key, None)
            elif e.kind in {"edit", "reaction"}:
                # Bounded orphan buffer; no attempt to import historical messages.
                pending = self.state["deferred"]
                if len(pending) < 1000:
                    pending.setdefault(key, []).append(e.json())
                    pending[key] = pending[key][-100:]
                else:
                    self.issue("unmapped_event_buffer_full", e.message_id)
            return
        if e.platform == "zulip" and e.message_id == message.get("topic_copy"):
            if e.kind == "delete":
                message["topic_copy_suppressed"] = True
                return
            if e.kind == "edit":
                return  # Bot-copy echoes are not edits of the human original.
        if message.get("out_of_scope"):
            return
        if e.kind == "delete":
            self.delete(e, message)
        elif e.kind == "edit":
            self.edit(e, message)
        elif e.kind == "reaction":
            self.react(e, message)

    def topic_root(self, topic: str) -> str:
        """Return the single bound Slack thread for a Zulip topic."""
        matches = [
            root for root, c in self.state["conversations"].items() if c["topic"] == topic and topic
        ]
        if len(matches) > 1:
            raise DeliveryError("ambiguous_topic")
        if matches and self.state["conversations"][matches[0]].get("blocked"):
            raise DeliveryError("conversation_needs_repair")
        return matches[0] if matches else ""

    def prepare_images(
        self,
        e: Event,
        previous: dict[str, Any] | None = None,
    ) -> tuple[str, str, list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        """Reuse confirmed uploads across edits, and provide visible unsupported-file fallbacks."""
        previous = previous or {}
        attachments = (
            e.attachments if e.attachments is not None else previous.get("attachments", [])
        )
        uploaded = dict(previous.get("uploaded_images", {}))
        body, suffix, images = e.text, "", []
        destination = "zulip" if e.platform == "slack" else "slack"
        for index, attachment in enumerate(attachments):
            identity = attachment["id"]
            digest = hashlib.sha256(identity.encode()).hexdigest()[:20]
            if index >= MAX_IMAGES:
                if attachment.get("markup"):
                    body = body.replace(attachment["markup"], "")
                if index == MAX_IMAGES:
                    suffix += "\n[More attachments — open original; five-image limit]"
                continue
            result = uploaded.get(identity)
            if result is None:
                try:
                    result = self.call(
                        e, f"image-{digest}", destination, "upload-image", attachment=attachment
                    )
                except DeliveryError as error:
                    if error.code not in {
                        "image_too_large",
                        "image_download_unavailable",
                        "slack_missing_scope",
                        "slack_file_not_found",
                        "slack_file_deleted",
                    } or error.category in {"uncertain", "retry"}:
                        raise
                    result = {
                        "skipped": "image unavailable",
                        "name": attachment.get("name", "image"),
                    }
                    self.issue(error.code, e.message_id)
                uploaded[identity] = result
            if attachment.get("markup"):
                body = body.replace(attachment["markup"], "")
            name = label(result.get("name", "image"))
            if result.get("skipped"):
                suffix += f"\n[Attachment: {name} — {result['skipped']}; open original]"
            elif destination == "zulip":
                suffix += f"\n[{name}]({result['url']})"
            else:
                images.append(result)
                suffix += f"\n[Image: {name}]"
        return body.strip(), suffix, images, uploaded, attachments

    def create(self, e: Event) -> None:
        """Mirror a new message and bind its conversation without per-user auth."""
        key = f"{e.platform}:{e.message_id}"
        if key in self.state["tombstones"]:
            return
        convs = self.state["conversations"]
        zulip_root = (
            self.topic_root(e.topic)
            if e.platform == "zulip" and e.topic != self.config.feed
            else ""
        )
        body, media_suffix, images, uploaded, attachments = self.prepare_images(e)
        content = (
            render(
                e.author or e.actor,
                e.platform,
                body,
                self.link(e.platform, e.message_id),
                text_format=e.text_format,
                limit=(
                    getattr(self.transport, "max_message_length", 9000)
                    if e.platform == "slack"
                    else 9000
                )
                - len(media_suffix),
            )
            + media_suffix
        )
        if e.platform == "slack":
            root = e.parent or e.message_id
            if e.parent and root != e.message_id:
                if root not in convs:
                    raise DeliveryError("missing_thread_parent")
                self.promote(e, root)
            if root in convs and convs[root].get("blocked"):
                raise DeliveryError("conversation_needs_repair")
            topic = convs.get(root, {}).get("topic") or self.config.feed
            result = self.call(e, "mirror", "zulip", "send", text=content, topic=topic)
            slack_id, zulip_id = e.message_id, str(result["id"])
        else:
            topic = e.topic
            root = zulip_root
            if topic != self.config.feed and not root:
                heading = self.topic_heading(topic)
                result = self.call(e, "topic-heading", "slack", "send", text=heading, parent="")
                root = str(result["id"])
                convs[root] = {
                    "topic": topic,
                    "parent_zulip": e.message_id,
                    "blocked": False,
                    "heading": heading,
                    "heading_echoes": [heading],
                }
            result = self.call(
                e, "mirror", "slack", "send", text=content, parent=root, images=images
            )
            slack_id, zulip_id = str(result["id"]), e.message_id
            root = root or slack_id
        if root not in convs:
            convs[root] = {
                "topic": "" if topic == self.config.feed else topic,
                "parent_zulip": zulip_id,
                "blocked": False,
            }
        message = {
            "origin": e.platform,
            "slack": slack_id,
            "zulip": zulip_id,
            "root": root,
            "author": e.author or e.actor,
            "text": e.text,
            "text_format": e.text_format,
            "attachments": attachments,
            "uploaded_images": uploaded,
            "media_suffix": media_suffix,
            "images": images,
            "revision": e.revision,
            "topic": topic,
            "deleted": False,
            "suppressed": False,
            "reactions": {"slack": {}, "zulip": {}},
            "expected": content,
            "echoes": [hashlib.sha256(content.encode()).hexdigest()],
        }
        self.state["messages"][key] = message
        self.state["index"][f"slack:{slack_id}"] = key
        self.state["index"][f"zulip:{zulip_id}"] = key
        for pending in self.state["deferred"].pop(key, []):
            self.apply(Event(**pending))

    def promote(self, e: Event, root: str) -> None:
        """Copy the parent into the topic and leave a navigation pointer in the feed."""
        conv = self.state["conversations"][root]
        if conv.get("blocked"):
            raise DeliveryError("conversation_needs_repair")
        if conv["topic"]:
            return
        parent = self.find("slack", root)
        if not parent or parent["deleted"] or parent["suppressed"]:
            raise DeliveryError("parent_deleted_or_suppressed")
        topic = topic_name(parent["text"], root, getattr(self.transport, "max_topic_length", 60))
        if self.topic_root(topic):
            raise DeliveryError("promotion_topic_collision")
        old_id = parent["zulip"]
        media_suffix = parent.get("media_suffix", "") if parent["origin"] == "slack" else ""
        content = (
            render(
                parent["author"],
                parent["origin"],
                parent["text"],
                self.link(parent["origin"], root if parent["origin"] == "slack" else old_id),
                limit=getattr(self.transport, "max_message_length", 9000) - len(media_suffix),
                destination="zulip",
                text_format=parent.get("text_format", ""),
            )
            + media_suffix
        )
        copied = self.call(e, "topic-copy", "zulip", "send", topic=topic, text=content)
        new_id = str(copied["id"])
        pointer = f"Discussion continued → [{label(topic)}]({self.link('zulip', new_id)})"
        linked = False
        if parent["origin"] == "slack":
            try:
                self.call(e, "link-parent", "zulip", "edit", id=old_id, text=pointer)
                linked = True
            except DeliveryError as error:
                if error.category != "denied":
                    raise
        if not linked:
            self.call(e, "feed-continuation", "zulip", "send", topic=self.config.feed, text=pointer)
            conv["fallback"] = True
        for emoji in set(parent["reactions"]["slack"]) | set(parent["reactions"]["zulip"]):
            if parent["reactions"]["slack"].get(emoji) or parent["reactions"]["zulip"].get(emoji):
                self.call(
                    e,
                    f"copy-reaction-{emoji}",
                    "zulip",
                    "react",
                    id=new_id,
                    emoji=emoji,
                    added=True,
                )
        parent["reaction_origin_zulip"] = old_id
        for emoji, members in parent["reactions"]["zulip"].items():
            parent["reactions"]["zulip"][emoji] = [f"{member}|{old_id}" for member in members]
        key = self.state["index"][f"slack:{root}"]
        self.state["index"][f"zulip:{new_id}"] = key
        if parent["origin"] == "slack":
            self.state.setdefault("feed_pointers", []).append(old_id)
            parent["zulip"] = new_id
            parent["expected"] = content
            parent["echoes"] = [hashlib.sha256(content.encode()).hexdigest()]
            parent.pop("continuation", None)
            parent["topic"] = topic
        else:
            parent["topic_copy"] = new_id
            parent["topic_copy_topic"] = topic
        conv["parent_zulip"] = new_id
        conv["topic"] = topic

    def edit(self, e: Event, m: dict[str, Any]) -> None:
        """Mirror only source edits, retaining message IDs, attribution and reactions."""
        if m["deleted"] or m["suppressed"]:
            return
        if e.platform != m["origin"]:
            hashes = {
                hashlib.sha256(t.encode()).hexdigest() for t in (e.text, html.unescape(e.text))
            }
            if not hashes.intersection(m.get("echoes", [])):
                m["suppressed"] = True
                self.issue("destination_edited_locally", e.message_id)
            return
        if Decimal(e.revision) < Decimal(m["revision"]):
            return
        if (
            e.text == m["text"]
            and e.text_format == m.get("text_format", "")
            and (e.attachments is None or e.attachments == m.get("attachments", []))
        ):
            return
        body, media_suffix, images, uploaded, attachments = self.prepare_images(e, m)
        destination: Platform = "zulip" if e.platform == "slack" else "slack"
        continuation = m.get("continuation", "") if destination == "zulip" else ""
        text = render(
            m["author"],
            e.platform,
            body,
            self.link(e.platform, e.message_id),
            m["topic"]
            if e.platform == "zulip" and m["topic"] != self.config.feed and m["slack"] == m["root"]
            else "",
            text_format=e.text_format,
            limit=(
                getattr(self.transport, "max_message_length", 9000) - len(continuation)
                if e.platform == "slack"
                else 8990
            )
            - len(media_suffix),
        )
        text += media_suffix + continuation
        if destination == "slack":
            text += "\n(edited)"
        try:
            self.call(
                e,
                "edit",
                destination,
                "edit",
                id=m[destination],
                text=text,
                **({"images": images} if destination == "slack" else {}),
            )
        except DeliveryError as error:
            if error.category != "denied":
                raise
            self.notice(e, m, destination, "edited")
            self.issue("edit_not_applied_notice_posted", e.message_id)
        if m.get("topic_copy") and not m.get("topic_copy_suppressed"):
            copy_text = render(
                m["author"],
                "zulip",
                e.text,
                self.link("zulip", m["zulip"]),
                limit=getattr(self.transport, "max_message_length", 9000),
                destination="zulip",
            )
            self.update_topic_copy(e, m, "edit", copy_text)
        m.update(
            text=e.text,
            text_format=e.text_format,
            revision=e.revision,
            expected=text,
            attachments=attachments,
            uploaded_images=uploaded,
            media_suffix=media_suffix,
            images=images,
        )
        m.setdefault("echoes", []).append(hashlib.sha256(text.encode()).hexdigest())

    def delete(self, e: Event, m: dict[str, Any]) -> None:
        """Source removal affects the mirror; destination moderation never deletes originals."""
        if m["deleted"]:
            return
        if e.platform != m["origin"]:
            m["suppressed"] = True
            self.issue("destination_removed_locally", e.message_id)
            return
        destination: Platform = "zulip" if e.platform == "slack" else "slack"
        if not m["suppressed"]:
            try:
                self.call(e, "delete", destination, "delete", id=m[destination])
            except DeliveryError as error:
                if error.category != "denied":
                    raise
                try:
                    self.call(
                        e,
                        "redact",
                        destination,
                        "edit",
                        id=m[destination],
                        text="Original message withdrawn (deleted or moved out of view).",
                    )
                except DeliveryError as redact_error:
                    if redact_error.category != "denied":
                        raise
                    self.notice(e, m, destination, "withdrawn (deleted or moved out of view)")
                    self.issue("withdrawal_copy_remains_notice_posted", e.message_id)
        if m.get("topic_copy") and not m.get("topic_copy_suppressed"):
            self.update_topic_copy(e, m, "delete")
        if e.platform == "zulip":
            self.issue("withdrawn_deleted_or_moved_out_of_view", e.message_id)
        m.update(deleted=True, text="", expected="", reactions={"slack": {}, "zulip": {}})
        m["echoes"] = []

    def update_topic_copy(self, e: Event, m: dict[str, Any], method: str, text: str = "") -> None:
        """Keep a copied Zulip original current without editing the human's feed message."""
        try:
            args = {"id": m["topic_copy"]}
            if method == "edit":
                args["text"] = text
            self.call(e, "update-topic-copy", "zulip", method, **args)
        except DeliveryError as error:
            if error.category != "denied":
                raise
            self.call(
                e,
                "topic-copy-notice",
                "zulip",
                "send",
                topic=m["topic_copy_topic"],
                text=(
                    "The original was changed or withdrawn; the old copy remains: "
                    f"{self.link('zulip', m['topic_copy'])}"
                ),
            )
            self.issue("topic_copy_change_denied", e.message_id)

    def notice(self, e: Event, m: dict[str, Any], destination: Platform, action: str) -> None:
        """Explain rejected corrections without repeating withdrawn or stale content."""
        text = (
            f"{label(m['author'])}'s original was {action} on {e.platform.title()}: "
            f"{self.link(e.platform, e.message_id)}\n"
            "The platform rejected updating this copy; its previous content remains."
        )
        routing = {"topic": m["topic"]} if destination == "zulip" else {"parent": m["root"]}
        self.call(e, "notice", destination, "send", text=text, **routing)

    def react(self, e: Event, m: dict[str, Any]) -> None:
        """Aggregate real remote reactors into one reaction owned by the destination bot."""
        if m["deleted"] or m["suppressed"] or not e.emoji:
            return
        old_id = m.get("reaction_origin_zulip")

        def copy_has_reactors() -> bool:
            return bool(m["reactions"]["slack"].get(e.emoji)) or any(
                member.endswith(f"|{old_id}") for member in m["reactions"]["zulip"].get(e.emoji, [])
            )

        copy_before = copy_has_reactors() if old_id else False
        members = set(m["reactions"][e.platform].get(e.emoji, []))
        before = bool(members)
        # A user can select both toned and untoned variants. Keep those memberships
        # distinct even though Zulip receives just the base codepoint reaction.
        member = f"{e.actor}|{e.reaction_variant or e.emoji}"
        if old_id and e.platform == "zulip":
            member += f"|{e.message_id}"
        if e.added:
            members.add(member)
        else:
            members.discard(member)
            members.discard(e.actor)  # Legacy state stored only actor IDs.
        if before != bool(members) and not (old_id and e.platform == "slack"):
            destination: Platform = "zulip" if e.platform == "slack" else "slack"
            self.call(
                e,
                "reaction",
                destination,
                "react",
                id=m.get("topic_copy", m[destination])
                if destination == "zulip"
                else m[destination],
                emoji=e.emoji,
                added=bool(members),
            )
        m["reactions"][e.platform][e.emoji] = sorted(members)
        if old_id and copy_before != copy_has_reactors():
            self.call(
                e,
                "reaction-topic",
                "zulip",
                "react",
                id=m.get("topic_copy", m["zulip"]),
                emoji=e.emoji,
                added=copy_has_reactors(),
            )

    def move(self, e: Event) -> None:
        """Accept unambiguous single-parent promotion and complete known-topic renames."""
        messages = [m for mid in e.ids if (m := self.find("zulip", mid)) is not None]
        if not messages:
            return
        if e.out_of_scope:
            for message in messages:
                message["out_of_scope"] = True
        roots = {m["root"] for m in messages}
        if not e.out_of_scope and all(m["topic"] == e.topic for m in messages):
            return  # Echo of our own completed move.
        destination_roots = {
            r for r, c in self.state["conversations"].items() if c["topic"] == e.topic and e.topic
        }
        conflict = e.out_of_scope or e.topic == self.config.feed or len(roots) != 1
        conflict = conflict or bool(destination_roots - roots)
        for root in roots:
            conv = self.state["conversations"][root]
            known = {
                m["zulip"]
                for m in self.state["messages"].values()
                if m["root"] == root and not m["deleted"] and m["topic"] == conv["topic"]
            }
            if conv["topic"] and not known.issubset(set(e.ids)):
                conflict = True
        if conflict:
            for root in roots | destination_roots:
                self.state["conversations"][root]["blocked"] = True
            self.issue("unsupported_topic_move", e.message_id)
            return
        root = next(iter(roots))
        conv = self.state["conversations"][root]
        if conv.get("heading"):
            heading = self.topic_heading(e.topic)
            self.call(e, "rename-heading", "slack", "edit", id=root, text=heading)
            conv.setdefault("heading_echoes", [conv["heading"]]).append(heading)
            conv["heading"] = heading
        conv["topic"] = e.topic
        for m in messages:
            m["topic"] = e.topic

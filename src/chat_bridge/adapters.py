"""SDK adapters: no automatic write retries and no secrets in raised errors."""

import hashlib
import json
import os
import time
from typing import Any

import zulip
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .config import Config, secret
from .content import EMOJI, ZULIP_NAMES, slack_message
from .events import zulip_bot_ids
from .media import transfer_image
from .model import DeliveryError, Platform
from .store import Store


class LiveTransport:
    """Use bot credentials for the configured channel pair only."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.slack = WebClient(token=secret("SLACK_BOT_TOKEN"), timeout=20, retry_handlers=[])
        self.zulip = self.new_zulip_client()
        self.last_slack_send = 0.0
        self.queue_id: str | None = None
        self.users: dict[str, dict[str, Any]] = {}
        self.zulip_bots: set[str] = set()

    def new_zulip_client(self) -> zulip.Client:
        """Use a separate SDK session for the receiver thread and mutation worker."""
        return zulip.Client(
            email=secret("ZULIP_BOT_EMAIL"),
            api_key=secret("ZULIP_API_KEY"),
            site=self.cfg.zulip_site,
            config_file=os.devnull,
            retry_on_errors=False,
            cert_bundle=os.environ.get("ZULIP_CA_BUNDLE"),
            insecure=False,
        )

    @staticmethod
    def check_zulip(data: dict[str, Any], mutation: bool = False) -> dict[str, Any]:
        """Classify errors without retaining the server's potentially sensitive text."""
        if data.get("result") == "success":
            return data
        code = str(data.get("code", "zulip_error"))
        if code in {"RATE_LIMIT_HIT"}:
            raise DeliveryError("zulip_rate_limited", "retry", float(data.get("retry-after", 5)))
        if data.get("status_code", 0) >= 500 or data.get("result") == "http-error":
            raise DeliveryError("zulip_server_error", "uncertain" if mutation else "retry")
        # Some permission failures use BAD_REQUEST, so inspect only for classification,
        # never include the message (which can contain text/addresses) in persisted errors.
        message = str(data.get("msg", "")).lower()
        if code in {"MOVE_MESSAGES_TIME_LIMIT_EXCEEDED", "FORBIDDEN"} or any(
            phrase in message
            for phrase in (
                "permission",
                "not allowed",
                "time limit",
                "too old",
                "cannot edit",
                "no longer edit",
                "no longer delete",
                "cannot delete",
            )
        ):
            raise DeliveryError("zulip_permission_or_age_limit", "denied")
        raise DeliveryError(f"zulip_{code.lower()}")

    def preflight(self) -> dict[str, Any]:
        """Read identity, membership and users; do not claim to prove write permissions."""
        try:
            auth = self.slack.auth_test()
            if auth["team_id"] != self.cfg.slack_team:
                raise ValueError("Slack token belongs to a different workspace")
            slack_bot = str(auth["user_id"])
            self.slack_bot = slack_bot
            self.slack_site = str(auth["url"]).rstrip("/")
            channel: dict[str, Any] = (
                self.slack.conversations_info(channel=self.cfg.slack_channel).get("channel") or {}
            )
            if not channel.get("is_member") or channel.get("is_archived"):
                raise ValueError("Slack bot must be a member of an active test channel")
            cursor = ""
            while True:
                page = self.slack_user_page(cursor)
                self.users.update({u["id"]: u for u in page.get("members") or []})
                cursor = page.get("response_metadata", {}).get("next_cursor", "")
                if not cursor:
                    break
            if not self.users.get(slack_bot, {}).get("is_bot"):
                raise ValueError("Use a Slack bot token, not a personal account")
            me = self.check_zulip(self.zulip.call_endpoint("users/me", method="GET"))
            if not me.get("is_bot"):
                raise ValueError("Use a Zulip Generic bot, not a personal account")
            if me.get("bot_type") not in {None, 1}:
                raise ValueError("Use a Generic Zulip bot, not a webhook bot")
            if me.get("is_admin") or me.get("is_owner"):
                raise ValueError("The bridge must not use a Zulip administrator bot")
            subscriptions = self.check_zulip(
                self.zulip.call_endpoint("users/me/subscriptions", method="GET")
            )["subscriptions"]
            if not any(s["stream_id"] == self.cfg.zulip_channel for s in subscriptions):
                raise ValueError("Subscribe the Zulip bot to the configured channel first")
            members = self.check_zulip(self.zulip.call_endpoint("users", method="GET"))["members"]
            self.zulip_bots = {str(u["user_id"]) for u in members if u.get("is_bot")}
            settings = self.check_zulip(self.zulip.call_endpoint("server_settings", method="GET"))
            self.feature_level = int(settings.get("zulip_feature_level", 0))
            self.policy = self.read_policy(int(me["user_id"]))
        except (ValueError, DeliveryError):
            raise
        except Exception:
            raise DeliveryError("preflight_connection_or_api_failure") from None
        return {
            "slack_bot": slack_bot,
            "zulip_bot": str(me["user_id"]),
            "zulip_version": settings.get("zulip_version"),
            "zulip_feature_level": settings.get("zulip_feature_level"),
            "membership": "verified",
            "zulip_policy": self.policy,
            "mutations": "unverified_until_live_smoke_test",
        }

    def slack_user_page(self, cursor: str) -> Any:
        """Retry only a rate-limited directory read, preserving the current page."""
        for attempt in range(4):
            try:
                return self.slack.users_list(limit=999, cursor=cursor)
            except SlackApiError as error:
                if error.response.get("error") != "ratelimited" or attempt == 3:
                    raise
                headers = {k.lower(): v for k, v in error.response.headers.items()}
                time.sleep(max(1, int(headers.get("retry-after", "60"))) + 1)
        raise AssertionError("Unreachable")

    @staticmethod
    def write_digest(value: Any) -> str:
        """Persist only hashes of destination snapshots, never extra message bodies."""
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def snapshot_write(self, platform: Platform, method: str, args: dict[str, Any]) -> dict:
        """Read the exact target before mutation; read failures are safe to retry."""
        try:
            if platform == "slack":
                try:
                    data = self.slack.reactions_get(
                        channel=self.cfg.slack_channel, timestamp=args["id"], full=True
                    )
                except SlackApiError as error:
                    if error.response.get("error") == "message_not_found":
                        return {"absent": True}
                    raise
                msg: dict[str, Any] = data["message"] or {}
                blocks = [
                    {k: v for k, v in b.items() if k != "block_id"} for b in msg.get("blocks", [])
                ]
                value = {"text": msg.get("text", ""), "blocks": blocks}
                return {"digest": self.write_digest(value)}
            data = self.zulip.call_endpoint(
                f"messages/{args['id']}",
                method="GET",
                request={"apply_markdown": False},
                timeout=20,
            )
            if data.get("code") == "BAD_REQUEST" and data.get("msg") == "Invalid message(s)":
                subscriptions = self.check_zulip(
                    self.zulip.call_endpoint("users/me/subscriptions", method="GET", timeout=20)
                )
                if any(
                    c["stream_id"] == self.cfg.zulip_channel for c in subscriptions["subscriptions"]
                ):
                    return {"absent": True}
            msg = self.check_zulip(data)["message"]
            return {
                "digest": self.write_digest(msg["content"]),
                "topic": self.write_digest(msg["subject"]),
                "channel": msg.get("stream_id"),
            }
        except DeliveryError as error:
            if error.category == "retry":
                raise
            raise DeliveryError("target_read_unavailable", "retry", 30) from None
        except Exception:
            raise DeliveryError("target_read_unavailable", "retry", 30) from None

    def reconcile_write(
        self, platform: Platform, method: str, args: dict[str, Any], baseline: dict | None
    ) -> str:
        """Confirm success or unchanged pre-write state; preserve intervening edits."""
        current = self.snapshot_write(platform, method, args)
        if method == "delete" and current.get("absent"):
            return "done"
        if not current.get("absent"):
            if method == "move" and current.get("topic") == self.write_digest(args["topic"]):
                return "done" if current.get("channel") == self.cfg.zulip_channel else "unknown"
            if method == "edit":
                desired: Any = args["text"]
                if platform == "slack":
                    desired = slack_message(args["text"])
                    desired = {"text": desired["text"], "blocks": desired["blocks"]}
                    for image in args.get("images", []):
                        desired["blocks"].append(
                            {
                                "type": "image",
                                "slack_file": {"id": image["id"]},
                                "alt_text": image["name"],
                            }
                        )
                if current.get("digest") == self.write_digest(desired):
                    return "done"
        return "retry" if baseline is not None and current == baseline else "unknown"

    def execute(self, platform: Platform, method: str, args: dict[str, Any]) -> dict[str, Any]:
        """Execute one journaled mutation; an unclassified network failure is uncertain."""
        try:
            if method == "upload-image":
                return transfer_image(self, platform, args["attachment"])
            if platform == "slack":
                return self.slack_write(method, args)
            return self.zulip_write(method, args)
        except DeliveryError:
            raise
        except SlackApiError as error:
            code = str(error.response.get("error", "slack_error"))
            validation = error.response.get("response_metadata", {}).get("messages", [])
            if (
                code == "invalid_blocks"
                and any("invalid slack file" in message for message in validation)
                and any(
                    time.time() - image.get("uploaded_at", 0) < 120
                    for image in args.get("images", [])
                )
            ):
                # Slack can finalize an upload before it is usable in image blocks.
                # This is an explicit rejection, so retry the message, not the upload.
                raise DeliveryError("slack_image_processing", "retry", 5) from None
            if code in {"already_reacted", "no_reaction"} and method == "react":
                return {}
            if code == "message_not_found" and method == "delete":
                return {}
            if error.response.status_code == 429 or code in {"ratelimited", "rate_limited"}:
                delay = error.response.headers.get("Retry-After", "5")
                raise DeliveryError("slack_rate_limited", "retry", float(delay)) from None
            if error.response.status_code >= 500 or code in {"internal_error", "fatal_error"}:
                raise DeliveryError("slack_server_error", "uncertain") from None
            category = (
                "denied"
                if code
                in {
                    "edit_window_closed",
                    "cant_update_message",
                    "cant_delete_message",
                    "no_permission",
                    "restricted_action",
                }
                else "failed"
            )
            raise DeliveryError(f"slack_{code}", category) from None
        except Exception:
            raise DeliveryError("write_connection_lost", "uncertain") from None

    def slack_write(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        """Write linked attribution and literal content into the configured channel."""
        base: dict[str, Any] = {"channel": self.cfg.slack_channel}
        if method in {"send", "edit"}:
            kwargs: dict[str, Any] = {
                **base,
                **slack_message(args["text"]),
                "parse": "none",
                "link_names": False,
            }
            for image in args.get("images", []):
                kwargs["blocks"].append(
                    {"type": "image", "slack_file": {"id": image["id"]}, "alt_text": image["name"]}
                )
            if method == "send":
                time.sleep(max(0, 1.05 - (time.monotonic() - self.last_slack_send)))
                self.last_slack_send = time.monotonic()
                result = self.slack.chat_postMessage(
                    **kwargs,
                    thread_ts=args.get("parent") or None,
                    unfurl_links=False,
                    unfurl_media=False,
                    metadata={
                        "event_type": "zulip_slack_bridge",
                        "event_payload": {"op_key": args["op_key"]},
                    }
                    if args.get("op_key")
                    else None,
                )
                return {"id": str(result["ts"])}
            self.slack.chat_update(**kwargs, ts=args["id"])
        elif method == "delete":
            self.slack.chat_delete(**base, ts=args["id"])
        elif method == "react":
            function = self.slack.reactions_add if args["added"] else self.slack.reactions_remove
            function(**base, timestamp=args["id"], name=args["emoji"])
        else:
            raise DeliveryError("unsupported_slack_operation")
        return {}

    def zulip_write(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        """Make explicit REST calls with SDK retries disabled."""
        if "text" in args and len(args["text"]) > getattr(self, "max_message_length", 10000):
            raise DeliveryError("zulip_message_length_exceeded")
        if "topic" in args and len(args["topic"]) > getattr(self, "max_topic_length", 60):
            raise DeliveryError("zulip_topic_length_exceeded")
        verb, request = "POST", {}
        endpoint = f"messages/{args.get('id', '')}"
        if method == "send":
            endpoint = "messages"
            request = {
                "type": "stream",
                "to": self.cfg.zulip_channel,
                "topic": args["topic"],
                "content": args["text"],
            }
        elif method == "edit":
            verb, request = "PATCH", {"content": args["text"]}
        elif method == "move":
            verb = "PATCH"
            request = {
                "topic": args["topic"],
                "propagate_mode": "change_one",
                "send_notification_to_old_thread": True,
                "send_notification_to_new_thread": False,
            }
        elif method == "delete":
            verb = "DELETE"
        elif method == "react":
            endpoint += "/reactions"
            verb = "POST" if args["added"] else "DELETE"
            name = args["emoji"]
            request = {
                "emoji_name": ZULIP_NAMES.get(name, name),
                "emoji_code": EMOJI[name],
                "reaction_type": "unicode_emoji",
            }
        else:
            raise DeliveryError("unsupported_zulip_operation")
        if method == "send" and args.get("op_key") and getattr(self, "queue_id", None):
            request.update(local_id=args["op_key"], queue_id=self.queue_id)
        data = self.zulip.call_endpoint(endpoint, method=verb, request=request, timeout=20)
        if method == "react" and data.get("code") in {
            "REACTION_ALREADY_EXISTS",
            "REACTION_DOES_NOT_EXIST",
        }:
            return {}
        checked = self.check_zulip(data, mutation=True)
        return {"id": str(checked["id"])} if method == "send" else {}

    def reconcile_sends(self, store: Store) -> None:
        """Look for positive Slack metadata matches; absence never authorizes a resend."""
        for send in store.uncertain_sends():
            if send["platform"] != "slack":
                continue  # Zulip local IDs exist only in the sending queue's echo.
            try:
                kwargs: dict[str, Any] = dict(
                    channel=self.cfg.slack_channel,
                    limit=100,
                    oldest=str(send["started"] - 60),
                    include_all_metadata=True,
                )
                matches = []
                cursor = ""
                for _ in range(10):
                    if cursor:
                        kwargs["cursor"] = cursor
                    if send["parent"]:
                        page = self.slack.conversations_replies(ts=send["parent"], **kwargs)
                    else:
                        page = self.slack.conversations_history(**kwargs)
                    matches.extend(
                        m
                        for m in page.get("messages", [])
                        if m.get("user") == self.slack_bot
                        and m.get("metadata", {}).get("event_type") == "zulip_slack_bridge"
                        and m.get("metadata", {}).get("event_payload", {}).get("op_key")
                        == send["token"]
                    )
                    cursor = page.get("response_metadata", {}).get("next_cursor", "")
                    if not cursor:
                        break
                if len(matches) == 1:
                    store.ingest([], receipts=[(send["token"], "slack", str(matches[0]["ts"]))])
            except Exception:
                # Missing permissions, rate limits, truncation and read failures are all
                # inconclusive. Keep the write uncertain without SDK payload logging.
                return

    def group_membership(self, value: Any, user_id: int) -> bool | None:
        """Evaluate group settings when bot access is supported; otherwise report unknown."""
        if isinstance(value, dict):
            if user_id in value.get("direct_members", []):
                return True
            results = [self.group_membership(g, user_id) for g in value.get("direct_subgroups", [])]
            return True if True in results else (None if None in results else False)
        if not isinstance(value, int) or self.feature_level < 496:
            return None
        try:
            result = self.check_zulip(
                self.zulip.call_endpoint(
                    f"user_groups/{value}/members/{user_id}",
                    method="GET",
                    request={"direct_member_only": False},
                )
            )
            return result.get("is_user_group_member")
        except Exception:
            return None

    def read_policy(self, user_id: int) -> dict[str, Any]:
        """Inspect actual policy windows and limits using a temporary data-only queue."""
        data = self.check_zulip(
            self.zulip.register(
                event_types=[],
                fetch_event_types=["realm", "subscription", "realm_user"],
            )
        )
        self.zulip_bots = getattr(self, "zulip_bots", set()) | zulip_bot_ids(data)
        try:
            channel = next(
                (
                    s
                    for s in data.get("subscriptions", [])
                    if s["stream_id"] == self.cfg.zulip_channel
                ),
                None,
            )
            if channel is None:
                raise ValueError("Configured Zulip subscription is missing from policy response")
            if channel.get("topics_policy") == "empty_topic_only":
                raise ValueError("Blocked: Zulip channel disables named topics")
            self.max_topic_length = int(data.get("max_topic_length", 60))
            self.max_message_length = int(data.get("max_message_length", 10000))
            if self.max_topic_length < 14 or len(self.cfg.feed) > self.max_topic_length:
                raise ValueError("Zulip topic length limit is too small for the configured bridge")
            if self.max_message_length < 512:
                raise ValueError(
                    "Zulip message length limit is too small for attribution and notices"
                )
            keys = [
                "realm_allow_message_editing",
                "realm_message_content_edit_limit_seconds",
                "realm_message_content_delete_limit_seconds",
                "realm_move_messages_within_stream_limit_seconds",
            ]
            policy = {key: data.get(key, "unknown") for key in keys}
            policy.update(
                max_topic_length=self.max_topic_length,
                max_message_length=self.max_message_length,
                topics_policy=channel.get("topics_policy", "unknown"),
                late_change_behavior="notice; existing policies unchanged",
            )
            groups = {}
            for scope, values in (("realm", data), ("channel", channel)):
                for key, value in values.items():
                    if key.endswith("_group") and ("delete" in key or "move" in key):
                        groups[f"{scope}:{key}"] = {
                            "setting": value,
                            "bot_is_member": self.group_membership(value, user_id),
                        }
            policy["permission_groups"] = groups
            for action in ("edit", "delete"):
                seconds = policy[f"realm_message_content_{action}_limit_seconds"]
                window = (
                    "unlimited"
                    if seconds is None or seconds == 0
                    else ("unknown" if seconds == "unknown" else f"{seconds} seconds")
                )
                policy[f"{action}_window"] = window
            return policy
        finally:
            # This queue carries no subscriptions to events and never starts forwarding.
            self.zulip.deregister(data["queue_id"], timeout=20)

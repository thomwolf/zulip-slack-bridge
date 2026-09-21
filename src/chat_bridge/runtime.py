"""Receive platform events, durably enqueue them, and drive a single ordered worker."""

import logging
import threading
import time
from typing import Any

from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.client import BaseSocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from .adapters import LiveTransport
from .config import Config, secret
from .engine import Engine, initial_state
from .events import slack_events, zulip_bot_ids, zulip_events
from .model import DeliveryError
from .store import Store


class Runtime:
    """Keep ingestion separate from slow API writes; no public webhook is required."""

    def __init__(
        self,
        cfg: Config,
        store: Store,
        transport: LiveTransport,
        identity: dict[str, Any],
        socket: Any = None,
    ) -> None:
        self.cfg, self.store, self.transport = cfg, store, transport
        self.engine = Engine(cfg, store, transport, identity["slack_bot"], identity["zulip_bot"])
        self.identity = identity
        self.stop = threading.Event()
        self.reader = transport.new_zulip_client()
        self.socket = socket or SocketModeClient(
            app_token=secret("SLACK_APP_TOKEN"), web_client=transport.slack, concurrency=1
        )
        if socket is None:
            self.socket.socket_mode_request_listeners.append(self.receive_slack)

    def receive_slack(self, client: BaseSocketModeClient, request: SocketModeRequest) -> None:
        """Acknowledge only after SQLite commits; ignore unrequested envelope types."""
        if request.type == "events_api":
            self.store.ingest(
                slack_events(request.payload, self.cfg, self.transport.users),
                envelope=(request.envelope_id, request.payload.get("event_id", "")),
            )
            self.store.set("slack_last_event", time.time())
        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))

    def register(self) -> dict[str, Any]:
        """Create an event queue; local filters enforce the configured channel boundary."""
        options: dict[str, Any] = {}
        if self.identity.get("zulip_feature_level", 0) >= 481:
            options["idle_queue_timeout"] = 604800
        data = self.transport.check_zulip(
            self.reader.register(
                event_types=[
                    "message",
                    "update_message",
                    "delete_message",
                    "reaction",
                    "realm_user",
                ],
                fetch_event_types=["realm_user"],
                apply_markdown=False,
                client_capabilities={
                    "notification_settings_null": False,
                    "bulk_message_deletion": True,
                },
                **options,
            )
        )
        self.transport.zulip_bots.update(zulip_bot_ids(data))
        self.store.set("queue_idle_timeout_seconds", data.get("idle_queue_timeout_secs", 600))
        cursor = {"queue_id": data["queue_id"], "last_event_id": data["last_event_id"]}
        self.store.ingest([], cursor)
        return cursor

    def receive_zulip(self) -> None:
        """Long-poll with a bounded timeout; stop visibly if replay history expires."""
        try:
            cursor = self.store.get("zulip_cursor") or self.register()
            while not self.stop.is_set():
                try:
                    data = self.reader.call_endpoint(
                        "events", method="GET", request=cursor, timeout=45
                    )
                except Exception:
                    self.store.set("health", {"zulip": "connection_retrying"})
                    self.stop.wait(3)
                    continue
                if data.get("code") == "BAD_EVENT_QUEUE_ID":
                    self.store.set(
                        "zulip_gap",
                        {
                            "from": self.store.get("zulip_last_poll"),
                            "to": time.time(),
                            "reason": "event_queue_expired",
                        },
                    )
                    self.store.set("zulip_cursor", None)
                    raise DeliveryError("zulip_queue_expired_accept_gap_required")
                try:
                    self.transport.check_zulip(data)
                except DeliveryError as error:
                    if error.category not in {"retry", "uncertain"}:
                        raise
                    self.store.set("health", {"zulip": "connection_retrying"})
                    self.stop.wait(min(60, max(1, error.retry_after)))
                    continue
                state = self.store.get("state", initial_state())
                known = {key.split(":", 1)[1] for key in state["index"] if key.startswith("zulip:")}
                known.update(self.store.get("zulip_seen", []))
                events = []
                receipts = []
                for raw in data.get("events", []):
                    # Include bot-posted copies immediately, even before the delivery
                    # worker commits its mapping. Their human reactions still matter.
                    if raw.get("type") == "realm_user":
                        person = raw.get("person", {})
                        if person.get("is_bot"):
                            self.transport.zulip_bots.add(str(person["user_id"]))
                    msg = raw.get("message", {})
                    if (
                        raw.get("local_message_id")
                        and str(msg.get("sender_id")) == self.identity["zulip_bot"]
                        and msg.get("stream_id") == self.cfg.zulip_channel
                    ):
                        receipts.append((str(raw["local_message_id"]), "zulip", str(msg["id"])))
                    if (
                        raw.get("type") == "message"
                        and msg.get("stream_id") == self.cfg.zulip_channel
                    ):
                        known.add(str(msg["id"]))
                    normalized = zulip_events(
                        raw, cursor["queue_id"], self.cfg, self.transport.zulip_bots, known
                    )
                    events.extend(normalized)
                    known.update(e.message_id for e in normalized if e.kind == "create")
                    cursor["last_event_id"] = max(cursor["last_event_id"], raw["id"])
                self.store.ingest(events, cursor, sorted(known), receipts)
                self.store.set("zulip_last_poll", time.time())
                self.store.set("health", {"zulip": "connected", "last_poll": time.time()})
        except DeliveryError as error:
            self.store.set("health", {"zulip": "stopped", "error": error.code})
            self.stop.set()
        except Exception:
            self.store.set("health", {"zulip": "stopped", "error": "receiver_failed"})
            self.stop.set()

    def prepare(self, accept_gap: bool = False) -> None:
        """Run until interrupted. Reconnect gaps require explicit operator acknowledgement."""
        if (self.store.get("last_run") or self.store.get("zulip_gap")) and not accept_gap:
            raise ValueError(
                "Restart may have missed Slack events. Inspect status, then use "
                "--accept-gap to continue; automatic history recovery is not built yet."
            )
        if accept_gap:
            self.store.set("gap_acknowledged_at", time.time())
            self.store.set("zulip_gap", None)
        self.store.recover()
        self.store.set("last_run", time.time())
        # SDK debug/error logs can include API payloads and credentials. Runtime health
        # is exported through sanitized status instead.
        for name in ("slack_sdk", "zulip", "urllib3"):
            logger = logging.getLogger(name)
            logger.handlers = [logging.NullHandler()]
            logger.propagate = False
        # Establish the queue before accepting Slack work or writing Zulip copies.
        if not self.store.get("zulip_cursor"):
            self.register()
        self.transport.queue_id = self.store.get("zulip_cursor")["queue_id"]

    def run(
        self, accept_gap: bool = False, *, prepared: bool = False, manage_socket: bool = True
    ) -> None:
        """Run one isolated pair, optionally using the coordinator's Slack connection."""
        if not prepared:
            self.prepare(accept_gap)
        thread = threading.Thread(target=self.receive_zulip, daemon=True)
        thread.start()
        try:
            if manage_socket:
                self.socket.connect()
            last_reconcile = 0.0
            while not self.stop.is_set():
                if time.monotonic() - last_reconcile >= 30:
                    self.transport.reconcile_sends(self.store)
                    last_reconcile = time.monotonic()
                self.store.wakeup.clear()
                if not self.engine.step():
                    self.store.idle_wait(self.stop)
            raise DeliveryError("receiver_stopped_check_status")
        finally:
            self.stop.set()
            if manage_socket:
                self.socket.close()
            thread.join(timeout=50)

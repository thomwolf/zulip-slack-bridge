"""A credential-free executable example; this is not a live-platform validation."""

import tempfile
from pathlib import Path
from typing import Any

from .config import Config
from .engine import Engine
from .model import Event, Platform
from .store import Store


class MemoryTransport:
    """Fake remote messages for deterministic examples and engine tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.messages: dict[str, dict[str, Any]] = {}
        self.counter = 100

    def execute(self, platform: Platform, method: str, args: dict[str, Any]) -> dict[str, Any]:
        """Record the visible effect and return a stable synthetic message ID."""
        self.calls.append((platform, method, dict(args)))
        if method == "send":
            self.counter += 1
            mid = f"{self.counter}.000001" if platform == "slack" else str(self.counter)
            self.messages[f"{platform}:{mid}"] = {**args, "reactions": []}
            return {"id": mid}
        key = f"{platform}:{args['id']}"
        message = self.messages.setdefault(key, {"reactions": []})
        if method == "delete":
            self.messages.pop(key, None)
        elif method == "react":
            reactions = set(message["reactions"])
            if args["added"]:
                reactions.add(args["emoji"])
            else:
                reactions.discard(args["emoji"])
            message["reactions"] = sorted(reactions)
        else:
            message.update(args)
        return {}


def run_demo(database: Path | None = None) -> dict[str, Any]:
    """Run real routing and SQLite persistence against fake platform APIs."""
    with tempfile.TemporaryDirectory(prefix="chat-bridge-demo-") as directory:
        path = database or Path(directory) / "demo.sqlite"
        if path.exists():
            raise ValueError("Choose a new demo database; the demo never overwrites existing state")
        store = Store(path)
        cfg = Config("TDEMO", "CDEMO", "https://zulip.example.test", 1, path)
        transport = MemoryTransport()
        engine = Engine(cfg, store, transport, "UBOT", "99")
        examples = [
            Event(
                "demo:1",
                "slack",
                "create",
                "1.000001",
                actor="UALICE",
                author="Alice",
                text="Anyone tried model X?",
                revision="1",
            ),
            Event(
                "demo:2",
                "slack",
                "create",
                "2.000001",
                actor="UBOB",
                author="Bob",
                text="Yes, yesterday.",
                parent="1.000001",
                revision="2",
            ),
            Event(
                "demo:3",
                "slack",
                "edit",
                "1.000001",
                actor="UALICE",
                text="Anyone tried model X locally?",
                revision="3",
            ),
            Event("demo:4", "slack", "reaction", "1.000001", actor="UBOB", emoji="thumbsup"),
            Event("demo:5", "slack", "reaction", "1.000001", actor="UCAROL", emoji="thumbsup"),
            Event(
                "demo:6",
                "zulip",
                "create",
                "900",
                actor="5",
                author="Dan",
                text="A new discussion from Zulip",
                topic="Plans",
                revision="6",
            ),
        ]
        store.ingest(examples + examples)  # Duplicate delivery is intentionally exercised.
        while engine.step():
            pass
        result = {
            "mode": "offline simulation — no Slack/Zulip connection",
            "status": store.status(),
            "conversations": engine.state["conversations"],
            "remote_messages": transport.messages,
        }
        store.close()
        return result

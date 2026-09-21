from pathlib import Path

import pytest

from chat_bridge.config import Config
from chat_bridge.demo import MemoryTransport
from chat_bridge.engine import Engine
from chat_bridge.model import Event
from chat_bridge.store import Store


class Harness:
    def __init__(self, path: Path) -> None:
        self.config = Config("T1", "C1", "https://zulip.test", 1, path)
        self.store = Store(path)
        self.transport = MemoryTransport()
        self.engine = Engine(self.config, self.store, self.transport, "UBOT", "99")
        self.count = 0

    def send(self, platform="slack", kind="create", mid="1.000001", **kwargs) -> Event:
        self.count += 1
        event = Event(f"test:{self.count}", platform, kind, mid, **kwargs)
        self.store.ingest([event])
        assert self.engine.step()
        return event

    def restart(self) -> None:
        self.store.close()
        self.store = Store(self.config.database)
        self.store.recover()
        self.engine = Engine(self.config, self.store, self.transport, "UBOT", "99")


@pytest.fixture
def bridge(tmp_path):
    harness = Harness(tmp_path / "bridge.sqlite")
    yield harness
    harness.store.close()

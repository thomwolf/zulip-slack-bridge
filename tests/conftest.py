from pathlib import Path

import libsql
import pytest

from chat_bridge.config import Config
from chat_bridge.demo import MemoryTransport
from chat_bridge.engine import Engine
from chat_bridge.model import Event
from chat_bridge.store import Store
from chat_bridge.turso import TursoConnection


class Harness:
    def __init__(self, path: Path, backend="sqlite") -> None:
        self.backend = backend
        self.claim = None
        self.config = Config("T1", "C1", "https://zulip.test", 1, path)
        self.store = self.open_store(path)
        self.transport = MemoryTransport()
        self.engine = Engine(self.config, self.store, self.transport, "UBOT", "99")
        self.count = 0

    def open_store(self, path):
        connection = (
            TursoConnection(
                libsql.connect(str(path), isolation_level=None, _check_same_thread=False)
            )
            if self.backend == "libsql"
            else None
        )
        store = Store(path, connection)
        if connection:
            self.claim = store.exclusive()
            self.claim.__enter__()
        return store

    def close(self):
        if self.claim:
            self.claim.__exit__(None, None, None)
            self.claim = None
        self.store.close()

    def send(self, platform="slack", kind="create", mid="1.000001", **kwargs) -> Event:
        self.count += 1
        event = Event(f"test:{self.count}", platform, kind, mid, **kwargs)
        self.store.ingest([event])
        assert self.engine.step()
        return event

    def restart(self) -> None:
        self.close()
        self.store = self.open_store(self.config.database)
        self.store.recover()
        self.engine = Engine(self.config, self.store, self.transport, "UBOT", "99")


@pytest.fixture(params=["sqlite", "libsql"])
def bridge(tmp_path, request):
    harness = Harness(tmp_path / "bridge.sqlite", request.param)
    yield harness
    harness.close()

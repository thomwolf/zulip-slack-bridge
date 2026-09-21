import libsql
import pytest

from chat_bridge.demo import MemoryTransport
from chat_bridge.store import Store
from chat_bridge.turso import StorageUnavailable, TursoConnection


def open_store(path):
    return Store(path, TursoConnection(libsql.connect(str(path), isolation_level=None)))


def test_only_one_remote_worker_can_claim_database(tmp_path):
    first = open_store(tmp_path / "shared.db")
    second = open_store(tmp_path / "shared.db")
    with first.exclusive():
        with pytest.raises(ValueError, match="already claimed"):
            with second.exclusive():
                pytest.fail("Concurrent worker entered")
        first.set("value", "first")
    with second.exclusive():
        assert second.get("value") == "first"
        second.set("value", "second")


def test_crashed_worker_requires_exact_owner_release(tmp_path):
    path = tmp_path / "shared.db"
    first = open_store(path)
    claim = first.exclusive()
    claim.__enter__()
    owner = first.status()["worker_owner"]["owner"]
    first.close()  # Simulate loss of the process without graceful lock release.
    claim.__exit__(None, None, None)
    second = open_store(path)
    with pytest.raises(ValueError, match="already claimed"):
        with second.exclusive():
            pass
    with pytest.raises(ValueError, match="changed"):
        second.db.release_abandoned("wrong-owner")
    second.db.release_abandoned(owner)
    with second.exclusive():
        second.set("recovered", True)


def test_transaction_rolls_back_business_error(tmp_path):
    store = open_store(tmp_path / "shared.db")
    with store.exclusive():
        with pytest.raises(ValueError):
            with store.db:
                store.db.execute("INSERT INTO meta VALUES ('test','true')")
                raise ValueError("abort")
        assert store.get("test") is None
        store.set("usable", True)


class FaultyDriver:
    def __init__(self, raw, after_commit=False):
        self.raw = raw
        self.after_commit = after_commit
        self.armed = False

    def execute(self, sql, params=()):
        if self.armed and sql == "COMMIT":
            if self.after_commit:
                self.raw.execute(sql, params)
            raise OSError("private-token-and-message-must-not-escape")
        return self.raw.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self.raw, name)


@pytest.mark.parametrize("after_commit", [False, True])
def test_unknown_commit_stops_before_chat_write_and_retains_owner(tmp_path, after_commit):
    path = tmp_path / "shared.db"
    faulty = FaultyDriver(libsql.connect(str(path), isolation_level=None), after_commit)
    store = Store(path, TursoConnection(faulty))
    transport = MemoryTransport()
    with store.exclusive():
        faulty.armed = True
        with pytest.raises(StorageUnavailable) as error:
            store.call("event/mirror", "slack", "send", {"text": "synthetic"}, transport)
        assert "private-token" not in str(error.value)
        assert transport.calls == []
        with pytest.raises(StorageUnavailable):
            store.get("anything")
    recovered = open_store(path)
    assert recovered.status()["worker_owner"]
    recovered.close()


def test_commit_response_loss_after_chat_send_preserves_journal(tmp_path):
    path = tmp_path / "shared.db"
    faulty = FaultyDriver(libsql.connect(str(path), isolation_level=None), True)
    store = Store(path, TursoConnection(faulty))
    transport = MemoryTransport()
    execute = transport.execute

    def send_then_break(*args):
        result = execute(*args)
        faulty.armed = True
        return result

    transport.execute = send_then_break
    with store.exclusive():
        with pytest.raises(StorageUnavailable):
            store.call("event/mirror", "slack", "send", {"text": "synthetic"}, transport)
    assert len(transport.calls) == 1
    recovered = open_store(path)
    owner = recovered.status()["worker_owner"]["owner"]
    recovered.db.release_abandoned(owner)
    with recovered.exclusive():
        result = recovered.call("event/mirror", "slack", "send", {"text": "synthetic"}, transport)
        assert result["id"]
        assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "file.db",
        "http://db.turso.io",
        "https://evil.test",
        "https://user:secret@db.turso.io",
        "https://db.turso.io?token=secret",
    ],
)
def test_refuse_non_remote_or_credential_bearing_urls(url):
    with pytest.raises(ValueError):
        TursoConnection.connect(url, "synthetic-token")

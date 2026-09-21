"""Direct libSQL access with fail-closed transactions and durable worker ownership."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast
from urllib.parse import urlparse
from uuid import uuid4

import libsql


class StorageUnavailable(RuntimeError):
    """The connection cannot safely continue; restart and inspect durable state."""


class Row:
    """Provide SQLite's indexed and named row access without copying store SQL."""

    def __init__(self, names: list[str], values: tuple) -> None:
        self.names, self.values = names, values

    def keys(self) -> list[str]:
        return self.names

    def __getitem__(self, key: str | int) -> Any:
        return self.values[self.names.index(key) if isinstance(key, str) else key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self.values)


class Cursor:
    """Materialize results while the serialized database operation is in progress."""

    def __init__(self, cursor: Any) -> None:
        self.rowcount = cursor.rowcount
        names = [c[0] for c in cursor.description or []]
        self.rows = [Row(names, row) for row in cursor.fetchall() or []]
        self.position = 0

    def fetchone(self) -> Row | None:
        if self.position == len(self.rows):
            return None
        row = self.rows[self.position]
        self.position += 1
        return row

    def fetchall(self) -> list[Row]:
        rows = self.rows[self.position :]
        self.position = len(self.rows)
        return rows

    def __iter__(self) -> Iterator[Any]:
        return iter(self.fetchall())


class TursoConnection:
    """Use a remote primary; never fall back to a local file or retry unknown commits."""

    def __init__(self, connection: Any) -> None:
        self.raw = connection
        self.failed = False
        self.closed = False
        self.owner: str | None = None

    @classmethod
    def connect(cls, url: str, token: str) -> "TursoConnection":
        """Validate an authenticated Turso endpoint without exposing it in errors."""
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"libsql", "https"}
            or not parsed.hostname
            or not parsed.hostname.endswith(".turso.io")
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Use a Turso libSQL database URL without credentials or query strings")
        if not token:
            raise ValueError("Missing environment variable: TURSO_AUTH_TOKEN")
        try:
            raw = cast(Any, libsql).connect(
                url, auth_token=token, isolation_level=None, _check_same_thread=False
            )
        except Exception:
            raise StorageUnavailable("Turso connection failed; details suppressed") from None
        return cls(raw)

    def _run(self, operation: Callable[..., Any], *args: Any) -> Any:
        if self.failed:
            raise StorageUnavailable("Turso connection stopped; inspect state before restarting")
        try:
            return operation(*args)
        except Exception:
            self.failed = True
            raise StorageUnavailable(
                "Turso operation failed or outcome unknown; worker stopped"
            ) from None

    def execute(self, sql: str, params: tuple = ()) -> Cursor:
        """Execute without replaying failures; driver errors may contain sensitive values."""
        return self._run(lambda: Cursor(self.raw.execute(sql, params)))

    def executescript(self, script: str) -> None:
        """Initialize fixed schema statements outside application transactions."""
        self._run(self.raw.executescript, script)

    def __enter__(self) -> "TursoConnection":
        if not self.owner:
            raise StorageUnavailable("Acquire Turso worker ownership before modifying state")
        self.execute("BEGIN IMMEDIATE")
        row = self.execute("SELECT owner FROM bridge_worker WHERE id=1").fetchone()
        if row is None or row[0] != self.owner:
            self.failed = True
            raise StorageUnavailable("Turso worker ownership lost; worker stopped")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        # If a request failed, its outcome may be unknown. Never reuse this session.
        if not self.failed:
            self.execute("ROLLBACK" if exc_type else "COMMIT")
        return False

    @contextmanager
    def ownership(self) -> Iterator[None]:
        """Claim a durable, non-expiring lock; crashes require explicit operator release."""
        owner = uuid4().hex
        self.execute("BEGIN IMMEDIATE")
        try:
            row = self.execute("SELECT owner FROM bridge_worker WHERE id=1").fetchone()
            if row:
                raise ValueError("Turso worker already claimed; inspect storage status")
            self.execute("INSERT INTO bridge_worker(id,owner) VALUES (1,?)", (owner,))
            self.execute("COMMIT")
        except BaseException:
            if not self.failed:
                self.execute("ROLLBACK")
            raise
        self.owner = owner
        try:
            yield
        finally:
            # On an ambiguous DB failure retain the claim until an operator has checked
            # the old process is stopped. No TTL allows a paused worker to overlap.
            if not self.failed:
                with self:
                    self.execute("DELETE FROM bridge_worker WHERE id=1 AND owner=?", (owner,))
            self.close()

    def release_abandoned(self, owner: str) -> None:
        """Remove only the exact owner an operator has verified is no longer running."""
        self.execute("BEGIN IMMEDIATE")
        changed = self.execute("DELETE FROM bridge_worker WHERE id=1 AND owner=?", (owner,))
        if changed.rowcount != 1:
            self.execute("ROLLBACK")
            raise ValueError("Worker owner changed or was already released; inspect status again")
        self.execute("COMMIT")

    def close(self) -> None:
        """Retire the connection permanently, including queued calls from receiver threads."""
        self.failed = True
        if not self.closed:
            self.closed = True
            self.raw.close()

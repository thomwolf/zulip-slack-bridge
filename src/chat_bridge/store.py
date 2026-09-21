"""Durable input and operation journals with short SQLite transactions."""

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .model import DeliveryError, Event, Platform, Transport


class Store:
    """Persist inputs before acknowledgement; commit routing state after delivery."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                next_attempt REAL NOT NULL DEFAULT 0, error TEXT, attempts INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sends (
                operation TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, platform TEXT NOT NULL,
                parent TEXT NOT NULL, started REAL NOT NULL, message_id TEXT
            );
            CREATE TABLE IF NOT EXISTS envelopes (
                envelope_id TEXT PRIMARY KEY, event_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations (
                key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                result TEXT, error TEXT, category TEXT
            );
        """)

    def close(self) -> None:
        """Close the database after receiver threads have stopped."""
        self.db.close()

    def get(self, key: str, default: Any = None) -> Any:
        """Read one metadata value."""
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value: Any) -> None:
        """Atomically replace a metadata value."""
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))

    def bind(self, identity: dict[str, Any]) -> None:
        """Refuse accidental reuse of a database for a different connection."""
        old = self.get("identity")
        if old is not None and old != identity:
            raise ValueError("Database belongs to a different channel pair, bot identity, or feed")
        self.set("identity", identity)

    def ingest(
        self,
        events: list[Event],
        cursor: dict[str, Any] | None = None,
        known_ids: list[str] | None = None,
        receipts: list[tuple[str, str, str]] | None = None,
        envelope: tuple[str, str] | None = None,
    ) -> None:
        """Commit events and the Zulip cursor together before acknowledging receipt."""
        with self.lock, self.db:
            if envelope is not None:
                inserted = self.db.execute("INSERT OR IGNORE INTO envelopes VALUES (?,?)", envelope)
                if inserted.rowcount == 0:
                    return
            for token, platform, message_id in receipts or []:
                self.db.execute(
                    "UPDATE sends SET message_id=? WHERE token=? AND platform=? "
                    "AND (message_id IS NULL OR message_id=?)",
                    (message_id, token, platform, message_id),
                )
            for event in events:
                self.db.execute(
                    "INSERT OR IGNORE INTO inbox(key,payload) VALUES (?,?)",
                    (event.key, json.dumps(event.json())),
                )
            if cursor is not None:
                self.db.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('zulip_cursor',?)",
                    (json.dumps(cursor),),
                )
            if known_ids is not None:
                self.db.execute(
                    "INSERT OR REPLACE INTO meta VALUES ('zulip_seen',?)",
                    (json.dumps(known_ids),),
                )

    def reconcile_receipts(self) -> None:
        """Release uncertain sends only after positive, durably recorded evidence."""
        with self.lock, self.db:
            rows = self.db.execute(
                "SELECT s.operation,s.message_id FROM sends s JOIN operations o "
                "ON s.operation=o.key WHERE s.message_id IS NOT NULL AND o.status='uncertain'"
            ).fetchall()
            for row in rows:
                self.db.execute(
                    "UPDATE operations SET status='done',result=?,error=NULL,category=NULL "
                    "WHERE key=?",
                    (json.dumps({"id": row["message_id"]}), row["operation"]),
                )
                # Replaying reconstructs uncommitted routing state; confirmed steps are reused.
                self.db.execute(
                    "UPDATE inbox SET status='pending',next_attempt=0,error=NULL "
                    "WHERE key=? AND status='uncertain'",
                    (row["operation"].rsplit("/", 1)[0],),
                )

    def uncertain_sends(self) -> list[dict[str, Any]]:
        """Return only correlation identifiers for bounded remote lookup."""
        with self.lock:
            return [
                dict(r)
                for r in self.db.execute(
                    "SELECT s.* FROM sends s JOIN operations o ON s.operation=o.key "
                    "WHERE o.status='uncertain' AND s.message_id IS NULL LIMIT 10"
                )
            ]

    def recover(self) -> None:
        """Never blindly repeat a write that was in flight when the process stopped."""
        with self.lock, self.db:
            self.db.execute(
                "UPDATE operations SET status='uncertain', error='interrupted_write',"
                " category='uncertain' WHERE status='running'"
            )

    def next(self) -> Event | None:
        """Return the oldest unfinished event; blocked work stops later delivery."""
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM inbox WHERE status != 'done' ORDER BY seq LIMIT 1"
            ).fetchone()
        if not row or row["status"] != "pending" or row["next_attempt"] > time.time():
            return None
        return Event(**json.loads(row["payload"]))

    def finish(self, event: Event, state: dict[str, Any]) -> None:
        """Commit the state transition and discard the processed event body together."""
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('state',?)", (json.dumps(state),))
            self.db.execute(
                "UPDATE inbox SET status='done', payload='{}', error=NULL WHERE key=?", (event.key,)
            )

    def fail(self, event: Event, error: DeliveryError) -> None:
        """Schedule definite retryable failures; hold uncertain or permanent errors."""
        with self.lock, self.db:
            self.db.execute(
                "UPDATE inbox SET status=?, error=?, next_attempt=?, "
                "attempts=attempts+1 WHERE key=?",
                (
                    "pending" if error.category == "retry" else error.category,
                    error.code,
                    time.time() + max(1, error.retry_after),
                    event.key,
                ),
            )

    def status(self) -> dict[str, Any]:
        """Return counts and sanitized error identifiers, never payloads or secrets."""
        with self.lock:
            counts = dict(self.db.execute("SELECT status, count(*) FROM inbox GROUP BY status"))
            held = [
                dict(r)
                for r in self.db.execute(
                    "SELECT key,status,error,attempts FROM inbox WHERE status!='done' "
                    "ORDER BY seq LIMIT 10"
                )
            ]
            ops = [
                dict(r)
                for r in self.db.execute(
                    "SELECT key,status,error FROM operations "
                    "WHERE status IN ('uncertain','running')"
                )
            ]
        return {
            "events": counts,
            "pending": held,
            "uncertain_operations": ops,
            "health": self.get("health", {}),
            "zulip_gap": self.get("zulip_gap"),
            "issues": self.get("state", {}).get("issues", []),
            "zulip_policy": self.get("zulip_policy", {}),
            "queue_idle_timeout_seconds": self.get("queue_idle_timeout_seconds"),
        }

    def retry(self, key: str) -> None:
        """Retry a known rejected event after repair, never an uncertain operation."""
        with self.lock, self.db:
            event_row = self.db.execute("SELECT status FROM inbox WHERE key=?", (key,)).fetchone()
            if not event_row or event_row[0] == "done":
                raise ValueError("Choose an unfinished event key from status")
            operations = self.db.execute("SELECT key,status FROM operations").fetchall()
            prefix = f"{key}/"
            if any(
                r["key"].startswith(prefix) and r["status"] in {"running", "uncertain"}
                for r in operations
            ):
                raise ValueError("Resolve uncertain writes against remote state before retrying")
            self.db.execute(
                "UPDATE inbox SET status='pending',next_attempt=0,error=NULL WHERE key=?"
                " AND status!='done'",
                (key,),
            )

    def resolve(self, key: str, delivered: bool, message_id: str | None = None) -> None:
        """Record an operator-verified remote outcome; do not infer success from a timeout."""
        with self.lock, self.db:
            row = self.db.execute("SELECT status FROM operations WHERE key=?", (key,)).fetchone()
            if not row or row[0] != "uncertain":
                raise ValueError("Choose an uncertain operation key from status")
            send_step = key.rsplit("/", 1)[-1] in {"mirror", "context", "breadcrumb", "notice"}
            if delivered and send_step and not message_id:
                raise ValueError("A confirmed send requires its actual destination message ID")
            result = {"id": message_id} if message_id else {}
            self.db.execute(
                "UPDATE operations SET status=?,result=?,error=NULL,category=NULL WHERE key=?",
                ("done" if delivered else "retry", json.dumps(result), key),
            )

    def call(
        self, key: str, platform: Platform, method: str, args: dict[str, Any], transport: Transport
    ) -> dict[str, Any]:
        """Journal a remote write and reuse confirmed responses during event replay."""
        fingerprint = hashlib.sha256(
            json.dumps([platform, method, args], sort_keys=True).encode()
        ).hexdigest()
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM operations WHERE key=?", (key,)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise DeliveryError("non_deterministic_operation")
                if row["status"] == "done":
                    return json.loads(row["result"])
                if row["status"] in {"uncertain", "running"}:
                    raise DeliveryError(row["error"] or "interrupted_write", "uncertain")
                # These rejections select a fallback branch. Preserve that decision
                # across replay; other permission failures can be retried after repair.
                if row["status"] == "denied" and key.rsplit("/", 1)[-1] in {
                    "move-parent",
                    "delete",
                    "edit",
                    "redact",
                }:
                    raise DeliveryError(row["error"], "denied")
            self.db.execute(
                "INSERT OR REPLACE INTO operations(key,fingerprint,status) VALUES (?,?,'running')",
                (key, fingerprint),
            )
        if method == "send":
            with self.lock, self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO sends(operation,token,platform,parent,started) "
                    "VALUES (?,?,?,?,?)",
                    (key, uuid.uuid4().hex, platform, args.get("parent", ""), time.time()),
                )
                token = self.db.execute(
                    "SELECT token FROM sends WHERE operation=?", (key,)
                ).fetchone()[0]
            args = {**args, "op_key": token}
        try:
            result = transport.execute(platform, method, args)
        except DeliveryError as error:
            with self.lock, self.db:
                self.db.execute(
                    "UPDATE operations SET status=?,error=?,category=? WHERE key=?",
                    (error.category, error.code, error.category, key),
                )
            raise
        except Exception:
            with self.lock, self.db:
                self.db.execute(
                    "UPDATE operations SET status='uncertain',error='unexpected_write_failure'"
                    " WHERE key=?",
                    (key,),
                )
            raise DeliveryError("unexpected_write_failure", "uncertain") from None
        with self.lock, self.db:
            self.db.execute(
                "UPDATE operations SET status='done',result=?,error=NULL WHERE key=?",
                (json.dumps(result), key),
            )
        return result

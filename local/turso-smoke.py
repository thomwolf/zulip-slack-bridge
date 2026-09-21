"""Check a real Turso database using synthetic data only; never contact chat APIs."""

import os
from uuid import uuid4

from chat_bridge.turso import TursoConnection


def main() -> None:
    """Check rollback and persistence after reconnect without altering bridge tables."""
    url, token = os.environ["TURSO_DATABASE_URL"], os.environ["TURSO_AUTH_TOKEN"]
    table = "bridge_probe_" + uuid4().hex
    connection = TursoConnection.connect(url, token)
    try:
        connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(f"INSERT INTO {table} VALUES (1,?)", ("synthetic durable probe",))
        connection.execute("COMMIT")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(f"INSERT INTO {table} VALUES (2,?)", ("must be rolled back",))
        connection.execute("ROLLBACK")
        connection.close()
        connection = TursoConnection.connect(url, token)
        rows = connection.execute(f"SELECT id,value FROM {table} ORDER BY id").fetchall()
        assert len(rows) == 1 and rows[0][0] == 1
        print("PASS: remote commit, rollback, and reconnect persistence. No chat APIs called.")
    finally:
        # A fresh session is needed after an ambiguous request; do not reuse a poisoned one.
        connection.close()
        cleanup = TursoConnection.connect(url, token)
        cleanup.execute(f"DROP TABLE IF EXISTS {table}")
        cleanup.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit(
            "Turso probe failed. Credentials and driver details were suppressed."
        ) from None

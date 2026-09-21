"""Space setup and explicitly enabled live forwarding with minimal health status."""

import json
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import load_pairs
from .demo import run_demo
from .multi import MultiRuntime, run_pairs
from .turso import TursoConnection

CHECKS: dict[str, bool] = {}
LIVE: MultiRuntime | None = None
MODE = "setup"


def forwarding_ready() -> bool:
    """Only report readiness after connection and while every worker is healthy."""
    if LIVE is None or not LIVE.socket.is_connected():
        return False
    try:
        return all(
            not r.stop.is_set()
            and r.store.get("health", {}).get("zulip") == "connected"
            and not r.store.delivery_blocked()
            for r in LIVE.runtimes
        )
    except Exception:
        return False


def observe_live(runtime: MultiRuntime) -> None:
    """Publish the running coordinator for read-only health checks."""
    global LIVE
    LIVE = runtime


PAGE = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Zulip–Slack bridge</title>
<style>
body{font:18px/1.65 system-ui,sans-serif;background:#f5f7fa;color:#172436;
max-width:680px;margin:10vh auto;padding:24px}h1{line-height:1.2}
article{background:white;padding:32px;border-radius:16px;border:1px solid #dde3eb}
small{color:#53657a}a{color:#1962b3}
</style><article><small>Hugging Face Science</small><h1>Zulip–Slack bridge</h1>
<p><strong>Deployment prepared · Forwarding not enabled</strong></p>
<p>The bridge software passed its offline startup check. The live bridge has not
been moved to this Space.</p>
<p>Before connecting, we need:</p><ul>
<li>A Zulip server reachable over public HTTPS.</li>
<li>Verified Turso credentials in this Space.</li>
<li>A verified handover from the current bridge.</li></ul>
<p>STARTUP_CHECKS</p><p>No messages or credentials are shown here.</p>
<a href="https://github.com/thomwolf/zulip-slack-bridge">Source code and setup guides</a>
</article></html>"""


def response(path: str) -> tuple[int, str, bytes]:
    """Separate container liveness from the unavailable forwarding readiness."""
    ready = forwarding_ready()
    if path == "/" and MODE != "setup":
        state = "Connected" if ready else "Starting or needs attention"
        return (
            200,
            "text/html; charset=utf-8",
            (
                "<!doctype html><title>Zulip–Slack bridge</title>"
                f"<h1>Zulip–Slack bridge</h1><p>{state}</p>"
                "<p>Live mode. No messages or credentials are shown here.</p>"
            ).encode(),
        )
    if path == "/":
        checks = "<br>".join(
            f"{label}: {'ready' if passed else 'not ready'}" for label, passed in CHECKS.items()
        )
        return 200, "text/html; charset=utf-8", PAGE.replace("STARTUP_CHECKS", checks).encode()
    if path in {"/healthz", "/readyz"}:
        return (
            200 if path == "/healthz" or ready else 503,
            "application/json",
            json.dumps({"container": "ok", "forwarding": ready, "mode": MODE}).encode(),
        )
    return 404, "text/plain", b"Not found"


class Handler(BaseHTTPRequestHandler):
    """Expose only fixed setup information; no configuration or state APIs."""

    def do_GET(self) -> None:
        """Serve the setup page and minimal health endpoints."""
        status, content_type, body = response(self.path)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Avoid logging request paths or user-supplied query strings."""


def deployment_checks() -> dict[str, bool]:
    """Check secret presence and read-only Turso connectivity; never start chat clients."""
    checks = {
        "Slack credentials": all(os.environ.get(k) for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")),
        "Turso connection": False,
        "Zulip credentials": all(os.environ.get(k) for k in ("ZULIP_BOT_EMAIL", "ZULIP_API_KEY")),
    }
    connection = None
    try:
        connection = TursoConnection.connect(
            os.environ.get("TURSO_DATABASE_URL", ""), os.environ.get("TURSO_AUTH_TOKEN", "")
        )
        row = connection.execute("SELECT 1").fetchone()
        checks["Turso connection"] = row is not None and row[0] == 1
    except Exception:
        pass  # Driver errors may contain secrets: report only the fixed status label.
    finally:
        if connection:
            connection.close()
    return checks


def main() -> None:
    """Serve health independently while running the bridge on the main thread."""
    global MODE, LIVE
    os.umask(0o077)
    run_demo(None)
    server = ThreadingHTTPServer(("0.0.0.0", 7860), Handler)
    if os.environ.get("BRIDGE_ENABLED") != "1":
        CHECKS.update(deployment_checks())
        server.serve_forever()
        return
    MODE = "live"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def stop(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        configs = load_pairs(Path("bridge.toml"))
        if any(c.storage_backend != "turso" for c in configs):
            raise ValueError("Live Spaces require durable Turso storage")
        run_pairs(
            configs,
            "run",
            accept_gap=os.environ.get("BRIDGE_ACCEPT_GAP") == "1",
            observe=observe_live,
        )
    except KeyboardInterrupt:
        print("Bridge stopped; durable state retained.", flush=True)
    except Exception:
        MODE = "failed"
        LIVE = None
        print("Bridge stopped. Inspect configuration and durable state.", flush=True)
        # Keep a non-ready status page available for operator diagnosis.
        threading.Event().wait()
    finally:
        LIVE = None
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

"""Space setup and explicitly enabled live forwarding with minimal health status."""

import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Config, load_pairs
from .demo import run_demo
from .model import DeliveryError
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
<p role="status"><strong>BRIDGE_STATUS</strong></p>
<p>Connects paired Slack and Zulip channels using shared bots. Messages appear
under their original author’s name, linked to the original post.</p>
<h2>Configuration</h2>
<p><a href="https://huggingface.co/spaces/science/zulipbridge/blob/main/bridge.toml"
>Edit bridge.toml on Hugging Face</a> — open the file, choose Edit, then commit
using an account with write access.</p>
<p>Each <code>[[pairs]]</code> entry selects a Slack channel ID, a Zulip channel ID,
and a <code>feed_topic</code> for standalone messages. Committing a change rebuilds
and restarts the Space; configuration is read at startup.</p>
<p><strong>Changing an existing pair needs a migration.</strong> The database is
bound to its channels, bots and feed topic. Editing those values alone stops
forwarding to protect existing message mappings. For a Slack-channel switch, pause
this Space and use the <code>switch-slack-channel</code> command in the
<a href="https://github.com/thomwolf/zulip-slack-bridge/blob/main/docs/huggingface-spaces.md"
>operator guide</a> to preview and apply an archived fresh start.
A separate additional pair needs its own database.</p>
<p>Keep tokens and API keys in
<a href="https://huggingface.co/spaces/science/zulipbridge/settings">Space Settings → Secrets</a>,
never in TOML. Turso stores message mappings and delivery state across restarts.</p>
<h2>How conversations sync</h2>
<ul><li>Standalone Slack posts go to the configured Zulip feed topic.</li>
<li>A Slack reply starts a Zulip topic with a copy of its parent message.</li>
<li>A Zulip topic becomes a Slack thread with its title as the heading.</li>
<li>Edits, deletions, reactions and supported images sync through the bots,
subject to each platform’s permissions. Messages sent during downtime are not backfilled.</li></ul>
<h2>Setup and source</h2>
<p>Invite the Slack bot to each paired channel and subscribe the Zulip bot to its
channel. Set the credentials in Secrets, configure the pairs, and enable forwarding
with <code>BRIDGE_ENABLED=1</code>. Check this page after every change:
“Forwarding connected” confirms readiness; a Space marked RUNNING alone does not.</p>
<p><a href="https://github.com/thomwolf/zulip-slack-bridge">GitHub repository</a> ·
<a href="https://github.com/thomwolf/zulip-slack-bridge/blob/main/docs/zulip-setup.md"
>Zulip setup</a> ·
<a href="https://github.com/thomwolf/zulip-slack-bridge/blob/main/docs/live-testing.md"
>Slack setup and testing</a> ·
<a href="https://github.com/thomwolf/zulip-slack-bridge/blob/main/docs/huggingface-spaces.md"
>Space setup</a></p>
<p>STARTUP_CHECKS</p><small>No messages or credentials are shown here.</small>
</article></html>"""


def response(path: str) -> tuple[int, str, bytes]:
    """Serve operator guidance and distinguish container liveness from forwarding readiness."""
    ready = forwarding_ready()
    if path == "/":
        state = (
            "Forwarding connected"
            if ready
            else {
                "setup": "Setup mode · Forwarding disabled",
                "failed": "Bridge stopped · Check configuration and logs",
            }.get(MODE, "Starting or needs attention · Forwarding not ready")
        )
        checks = (
            "<br>".join(
                f"{label}: {'ready' if passed else 'not ready'}" for label, passed in CHECKS.items()
            )
            if MODE == "setup"
            else ""
        )
        page = PAGE.replace("BRIDGE_STATUS", state).replace("STARTUP_CHECKS", checks)
        return 200, "text/html; charset=utf-8", page.encode()
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


def run_live(configs: list[Config]) -> None:
    """Retry only preflight outages after run_pairs has cleanly released ownership."""
    delay = 5.0
    while True:
        try:
            run_pairs(
                configs,
                "run",
                accept_gap=os.environ.get("BRIDGE_ACCEPT_GAP") == "1",
                observe=observe_live,
            )
            return
        except DeliveryError as error:
            if error.code != "preflight_retryable" or error.category != "retry":
                raise
            print("Platform preflight temporarily unavailable; retrying.", flush=True)
            time.sleep(max(delay, error.retry_after))
            delay = min(60, delay * 2)


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
        run_live(configs)
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

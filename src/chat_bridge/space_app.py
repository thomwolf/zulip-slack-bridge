"""Credential-free deployment check; intentionally never starts live forwarding."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .demo import run_demo
from .turso import TursoConnection

CHECKS: dict[str, bool] = {}

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
    if path == "/":
        checks = "<br>".join(
            f"{label}: {'ready' if passed else 'not ready'}" for label, passed in CHECKS.items()
        )
        return 200, "text/html; charset=utf-8", PAGE.replace("STARTUP_CHECKS", checks).encode()
    if path in {"/healthz", "/readyz"}:
        return (
            200 if path == "/healthz" else 503,
            "application/json",
            json.dumps({"container": "ok", "forwarding": False, "mode": "setup"}).encode(),
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
    """Verify the real engine with fake transports before serving setup status."""
    run_demo(None)
    CHECKS.update(deployment_checks())
    print(json.dumps(CHECKS))
    print("Offline engine check passed. Setup server starting; forwarding disabled.")
    ThreadingHTTPServer(("0.0.0.0", 7860), Handler).serve_forever()


if __name__ == "__main__":
    main()

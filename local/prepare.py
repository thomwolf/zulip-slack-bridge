"""Prepare a loopback-only Zulip fixture without printing or replacing credentials."""

import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UPSTREAM_REVISION = "4e575b63ba2fdbaff1f2a6d1409d15ebfe4e6202"


def main() -> None:
    """Download the pinned official Compose file, generate secrets and local TLS."""
    os.umask(0o077)
    upstream = ROOT / "upstream"
    if not upstream.exists():
        subprocess.run(
            ["git", "clone", "https://github.com/zulip/docker-zulip.git", str(upstream)], check=True
        )
        subprocess.run(["git", "-C", str(upstream), "checkout", UPSTREAM_REVISION], check=True)
    actual = subprocess.check_output(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != UPSTREAM_REVISION:
        raise SystemExit("Unexpected upstream revision; inspect it before changing the fixture")
    secret_dir, tls_dir = ROOT / "secrets", ROOT / "tls"
    secret_dir.mkdir(exist_ok=True)
    tls_dir.mkdir(exist_ok=True)
    names = [
        "postgres_password",
        "memcached_password",
        "rabbitmq_password",
        "redis_password",
        "secret_key",
        "email_password",
    ]
    for name in names:
        path = secret_dir / name
        if not path.exists():
            path.write_text(secrets.token_hex(32))
    cert, key = tls_dir / "zulip.combined-chain.crt", tls_dir / "zulip.key"
    if not cert.exists():
        openssl = "/opt/homebrew/opt/openssl@3/bin/openssl"
        if not Path(openssl).exists():
            openssl = shutil.which("openssl") or "openssl"
        subprocess.run(
            [
                openssl,
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "90",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-subj",
                "/CN=zulip.localhost",
                "-addext",
                "subjectAltName=DNS:zulip.localhost,DNS:localhost,IP:127.0.0.1",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    cert.chmod(0o644)
    config = "secrets:\n" + "".join(
        f"  zulip__{name}:\n    file: {json.dumps(str(secret_dir / name))}\n" for name in names
    )
    config += f"""services:
  zulip:
    ports: !override
      - "127.0.0.1:8443:443"
    environment:
      SETTING_EXTERNAL_HOST: "zulip.localhost:8443"
      SETTING_ZULIP_ADMINISTRATOR: "admin@bridge.test"
      SETTING_EMAIL_BACKEND: "django.core.mail.backends.console.EmailBackend"
      SETTING_ZULIP_SERVICE_PUSH_NOTIFICATIONS: "False"
      SETTING_ZULIP_SERVICE_SUBMIT_USAGE_STATISTICS: "False"
      CERTIFICATES: "manual"
    volumes:
      - {json.dumps(str(tls_dir) + ":/data/certs/manual:ro")}
"""
    (ROOT / "compose.override.yaml").write_text(config)
    print("Local configuration prepared; credentials were not displayed.")


if __name__ == "__main__":
    main()

"""Operator CLI; remote status requires database credentials, never chat credentials."""

import argparse
import json
import os
from pathlib import Path

from .config import Config, secret
from .model import DeliveryError
from .store import Store
from .turso import TursoConnection


def main() -> None:
    """Run preflight, the service, offline demo, or inspect local status."""
    parser = argparse.ArgumentParser(description="Experimental single-channel Slack/Zulip bridge")
    parser.add_argument("--config", type=Path, default=Path("bridge.toml"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Read-only identity and channel membership check")
    run = commands.add_parser(
        "run", help="Start live forwarding; requires configured test channels"
    )
    run.add_argument("--accept-gap", action="store_true", help="Acknowledge missing offline events")
    commands.add_parser("status", help="Show durable delivery status without connecting")
    retry = commands.add_parser("retry", help="Retry a rejected event after repairing its cause")
    retry.add_argument("event_key")
    resolve = commands.add_parser("resolve", help="Record a verified outcome of an uncertain write")
    resolve.add_argument("operation_key")
    outcome = resolve.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--delivered", action="store_true")
    outcome.add_argument("--not-delivered", action="store_true")
    resolve.add_argument("--message-id", help="Destination message ID for a confirmed send")
    release = commands.add_parser("release-owner", help="Release an abandoned Turso worker claim")
    release.add_argument("owner")
    release.add_argument("--confirm-worker-stopped", action="store_true", required=True)
    demo = commands.add_parser("demo", help="Exercise the engine locally with fake platforms")
    demo.add_argument("--database", type=Path, default=None)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "demo":
            from .demo import run_demo

            print(json.dumps(run_demo(args.database), indent=2))
            return
        cfg = Config.load(args.config)
        connection = (
            TursoConnection.connect(secret("TURSO_DATABASE_URL"), secret("TURSO_AUTH_TOKEN"))
            if cfg.storage_backend == "turso"
            else None
        )
        store = Store(cfg.database, connection)
        if args.command == "release-owner":
            if connection is None:
                raise ValueError("release-owner is only for Turso")
            connection.release_abandoned(args.owner)
            connection.close()
            print("Abandoned owner released. Inspect status before restarting.")
            return
        if args.command == "status":
            output = store.status()
            output["issues"] = store.get("state", {}).get("issues", [])
            print(json.dumps(output, indent=2))
            return
        with store.exclusive():
            if args.command == "retry":
                store.retry(args.event_key)
                print("Event queued; uncertain writes are never retried automatically.")
                return
            if args.command == "resolve":
                store.resolve(args.operation_key, args.delivered, args.message_id)
                print(
                    "Outcome recorded. Retry the held event after resolving all uncertain writes."
                )
                return
            from .adapters import LiveTransport

            transport = LiveTransport(cfg)
            identity = transport.preflight()
            store.set("zulip_policy", identity["zulip_policy"])
            print(json.dumps(identity, indent=2))
            if args.command == "check":
                return
            store.bind(
                {
                    **cfg.identity(),
                    "slack_bot": identity["slack_bot"],
                    "zulip_bot": identity["zulip_bot"],
                }
            )
            from .runtime import Runtime

            Runtime(cfg, store, transport, identity).run(args.accept_gap)
    except KeyboardInterrupt:
        print("Stopped. Delivery state is saved.")
    except (ValueError, DeliveryError) as error:
        parser.exit(1, f"Bridge: {error}\n")
    except Exception:
        parser.exit(
            1, "Bridge: unexpected failure; inspect local status. Secrets were not logged.\n"
        )


if __name__ == "__main__":
    main()

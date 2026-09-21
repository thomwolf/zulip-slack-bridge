"""Small operator CLI; status and the offline demo require no credentials."""

import argparse
import fcntl
import json
import os
from pathlib import Path

from .config import Config
from .model import DeliveryError
from .store import Store


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
        store = Store(cfg.database)
        if args.command == "status":
            output = store.status()
            output["issues"] = store.get("state", {}).get("issues", [])
            print(json.dumps(output, indent=2))
            return
        # flock prevents two workers, including an operator retry during a running service.
        with cfg.database.with_suffix(".lockfile").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError(
                    "Another bridge process holds this database; stop it first"
                ) from None
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

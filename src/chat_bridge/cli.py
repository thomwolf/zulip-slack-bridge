"""Operator CLI; remote status requires database credentials, never chat credentials."""

import argparse
import json
import os
from pathlib import Path

from .config import load_pairs
from .model import DeliveryError
from .multi import open_store, run_pairs
from .turso import TursoConnection


def main() -> None:
    """Run preflight, the service, offline demo, or inspect local status."""
    parser = argparse.ArgumentParser(description="Experimental Slack/Zulip channel-pair bridge")
    parser.add_argument("--config", type=Path, default=Path("bridge.toml"))
    parser.add_argument("--pair", help="Select a pair for status or recovery; run starts all pairs")
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
    switch = commands.add_parser(
        "switch-slack-channel", help="Preview an archived fresh start for a new Slack channel"
    )
    switch.add_argument("--from-channel", required=True, help="Current stored Slack channel ID")
    switch.add_argument("--apply", action="store_true", help="Archive old state and start fresh")
    switch.add_argument("--confirm-worker-stopped", action="store_true")
    switch.add_argument("--accept-gap", action="store_true")
    demo = commands.add_parser("demo", help="Exercise the engine locally with fake platforms")
    demo.add_argument("--database", type=Path, default=None)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "demo":
            from .demo import run_demo

            print(json.dumps(run_demo(args.database), indent=2))
            return
        configs = load_pairs(args.config)
        if args.pair:
            if args.command == "run" and len(configs) > 1:
                raise ValueError("Run all configured pairs together to share the Slack socket")
            configs = [c for c in configs if c.pair_id == args.pair]
            if not configs:
                raise ValueError("Unknown pair id")
        if args.command == "switch-slack-channel":
            if len(configs) != 1:
                raise ValueError("Select exactly one pair with --pair")
            if args.apply and not (args.confirm_worker_stopped and args.accept_gap):
                raise ValueError("Apply requires --confirm-worker-stopped and --accept-gap")
        if len(configs) > 1:
            print(
                json.dumps(
                    run_pairs(configs, args.command, getattr(args, "accept_gap", False)), indent=2
                )
            )
            return
        cfg = configs[0]
        store = open_store(cfg)
        connection = store.db if isinstance(store.db, TursoConnection) else None
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
            if args.command == "switch-slack-channel":
                from .migration import switch_slack_channel

                result = switch_slack_channel(
                    store,
                    {
                        **cfg.identity(),
                        "slack_bot": identity["slack_bot"],
                        "zulip_bot": identity["zulip_bot"],
                    },
                    args.from_channel,
                    apply=args.apply,
                )
                print(json.dumps(result, indent=2))
                return
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

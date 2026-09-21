"""Exercise two live local Zulip channels with one bot and simulated Slack ingress."""

import json
import os
import tempfile
import threading
import time
from pathlib import Path

import zulip
from slack_sdk.socket_mode.request import SocketModeRequest

from chat_bridge.adapters import LiveTransport
from chat_bridge.config import Config
from chat_bridge.demo import MemoryTransport
from chat_bridge.model import DeliveryError
from chat_bridge.multi import MultiRuntime
from chat_bridge.runtime import Runtime
from chat_bridge.store import Store

ROOT = Path(__file__).resolve().parent


def main() -> None:
    """Create fixture channels and verify routing, reverse events and failure isolation."""
    credentials = json.loads((ROOT / "credentials.json").read_text())
    assert credentials["site"] == "https://zulip.localhost:8443"

    def client(name):
        return zulip.Client(
            email=credentials[name]["email"],
            api_key=credentials[name]["api_key"],
            site=credentials["site"],
            config_file=os.devnull,
            retry_on_errors=False,
            cert_bundle=str(ROOT / "tls/zulip.combined-chain.crt"),
        )

    checked = LiveTransport.check_zulip
    admin, alice = client("admin"), client("alice")
    names = ["bridge-multi-a", "bridge-multi-b"]
    checked(
        admin.call_endpoint(
            "users/me/subscriptions",
            request={
                "subscriptions": [{"name": name} for name in names],
                "principals": [credentials[n]["user_id"] for n in ("admin", "alice", "bob", "bot")],
            },
        )
    )
    subs = checked(client("bot").call_endpoint("users/me/subscriptions", method="GET"))[
        "subscriptions"
    ]
    ids = [next(s["stream_id"] for s in subs if s["name"] == name) for name in names]
    feature = checked(client("bot").call_endpoint("server_settings", method="GET"))[
        "zulip_feature_level"
    ]

    class Socket:
        def __init__(self):
            self.socket_mode_request_listeners = []
            self.ready = threading.Event()
            self.acks = []

        def connect(self):
            self.ready.set()

        def close(self):
            pass

        def send_socket_mode_response(self, response):
            self.acks.append(response.envelope_id)

    class Hybrid:
        check_zulip = staticmethod(checked)
        slack_site = "https://example.slack.com"

        def __init__(self, cfg):
            self.real = object.__new__(LiveTransport)
            self.real.cfg, self.real.zulip = cfg, client("bot")
            self.real.feature_level = feature
            self.real.read_policy(credentials["bot"]["user_id"])
            self.zulip_bots = self.real.zulip_bots
            self.max_message_length = self.real.max_message_length
            self.max_topic_length = self.real.max_topic_length
            self.users = {}
            self.fake = MemoryTransport()
            self.block = False
            self.queue_id = None

        def new_zulip_client(self):
            return client("bot")

        def reconcile_sends(self, store):
            pass

        def execute(self, platform, method, args):
            if platform == "zulip":
                self.real.queue_id = self.queue_id
                return self.real.execute(platform, method, args)
            if self.block:
                raise DeliveryError("synthetic_pair_failure", "denied")
            return self.fake.execute(platform, method, args)

    def wait(predicate):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.1)
        raise AssertionError("Local multipair check timed out")

    stamp = str(int(time.time()))
    with tempfile.TemporaryDirectory(prefix="bridge-multi-") as directory:
        socket = Socket()
        runtimes = []
        for n in range(2):
            cfg = Config(
                "TLOCAL",
                f"CLOCAL{n}",
                credentials["site"],
                ids[n],
                Path(directory) / f"{n}.sqlite",
                feed=f"Feed {n}",
                pair_id=f"pair{n}",
            )
            runtime = Runtime(
                cfg,
                Store(cfg.database),
                Hybrid(cfg),
                {
                    "slack_bot": "UBOT",
                    "zulip_bot": str(credentials["bot"]["user_id"]),
                    "zulip_feature_level": feature,
                },
                socket=socket,
            )
            runtimes.append(runtime)
        multi = MultiRuntime(runtimes, socket)
        errors = []

        def run():
            try:
                multi.run()
            except RuntimeError:
                pass  # Expected once we stop both workers at the end.
            except Exception as error:
                errors.append(type(error).__name__)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            assert socket.ready.wait(20), "Coordinator did not start"

            def slack(n, event, ts, text, parent=""):
                req = SocketModeRequest(
                    type="events_api",
                    envelope_id=event,
                    payload={
                        "team_id": "TLOCAL",
                        "event_id": event,
                        "event": {
                            "type": "message",
                            "channel": f"CLOCAL{n}",
                            "user": "UTEST",
                            "ts": ts,
                            "text": text,
                            "thread_ts": parent,
                        },
                    },
                )
                multi.receive_slack(socket, req)

            for n in range(2):
                slack(n, f"{stamp}-{n}", f"{stamp}.000001", f"Pair {n} root")
            wait(
                lambda: all(
                    f"slack:{stamp}.000001" in r.store.get("state", {}).get("index", {})
                    for r in runtimes
                )
            )
            for n, r in enumerate(runtimes):
                state = r.store.get("state")
                msg = state["messages"][state["index"][f"slack:{stamp}.000001"]]
                actual = checked(
                    client("bot").call_endpoint(f"messages/{msg['zulip']}", method="GET")
                )["message"]
                assert actual["stream_id"] == ids[n] and f"Pair {n} root" in actual["content"]
            slack(0, f"{stamp}-reply", f"{stamp}.000002", "Reply only in A", f"{stamp}.000001")
            wait(lambda: f"slack:{stamp}.000002" in runtimes[0].store.get("state")["index"])
            assert f"slack:{stamp}.000002" not in runtimes[1].store.get("state")["index"]
            originals = []
            for n in range(2):
                mid = checked(
                    alice.send_message(
                        {
                            "type": "stream",
                            "to": ids[n],
                            "topic": f"Local topic {stamp}",
                            "content": f"Zulip source {n}",
                        }
                    )
                )["id"]
                originals.append(mid)
            wait(
                lambda: all(
                    f"zulip:{mid}" in r.store.get("state")["index"]
                    for mid, r in zip(originals, runtimes, strict=True)
                )
            )
            assert f"zulip:{originals[0]}" not in runtimes[1].store.get("state")["index"]
            checked(
                alice.update_message({"message_id": originals[1], "content": "Edited only in B"})
            )
            wait(
                lambda: any(
                    "Edited only in B" in str(m)
                    for m in runtimes[1].transport.fake.messages.values()
                )
            )
            runtimes[0].transport.block = True
            checked(
                alice.send_message(
                    {
                        "type": "stream",
                        "to": ids[0],
                        "topic": f"Local topic {stamp}",
                        "content": "Intentional blocked test",
                    }
                )
            )
            wait(lambda: runtimes[0].store.status()["events"].get("denied", 0) > 0)
            mid = checked(
                alice.send_message(
                    {
                        "type": "stream",
                        "to": ids[1],
                        "topic": f"Local topic {stamp}",
                        "content": "B continues while A is held",
                    }
                )
            )["id"]
            wait(lambda: f"zulip:{mid}" in runtimes[1].store.get("state")["index"])
            print(
                "PASS: two real Zulip channels, one shared bot, isolated identical Slack IDs, "
                "thread promotion, reverse messages, edits, and pair failure isolation."
            )
            print("Slack transport simulated; no live Slack connection was opened.")
        finally:
            for r in runtimes:
                r.stop.set()
                r.store.wakeup.set()
                cursor = r.store.get("zulip_cursor")
                if cursor:
                    client("bot").call_endpoint(
                        "events", method="DELETE", request={"queue_id": cursor["queue_id"]}
                    )
            thread.join(timeout=55)
            assert not thread.is_alive(), "Worker shutdown timed out"
            for r in runtimes:
                r.store.close()
            assert not errors, errors


if __name__ == "__main__":
    main()

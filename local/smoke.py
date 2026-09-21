"""Test real local Zulip with the actual bridge engine and simulated Slack only."""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import zulip

from chat_bridge.adapters import LiveTransport
from chat_bridge.config import Config
from chat_bridge.demo import MemoryTransport
from chat_bridge.engine import Engine
from chat_bridge.events import zulip_events
from chat_bridge.model import Event
from chat_bridge.store import Store

ROOT = Path(__file__).resolve().parent


def main() -> None:
    """Create disposable test channels, exercise the APIs, and save a credential-free report."""
    credentials = json.loads((ROOT / "credentials.json").read_text())
    assert credentials["site"] == "https://zulip.localhost:8443"
    clients = {
        name: zulip.Client(
            email=credentials[name]["email"],
            api_key=credentials[name]["api_key"],
            site=credentials["site"],
            config_file=os.devnull,
            retry_on_errors=False,
            cert_bundle=str(ROOT / "tls/zulip.combined-chain.crt"),
        )
        for name in ("admin", "alice", "bob", "bot")
    }
    admin, alice, bot = clients["admin"], clients["alice"], clients["bot"]
    checked = LiveTransport.check_zulip
    stamp = str(int(time.time()))
    name = "bridge-test"
    checked(
        admin.call_endpoint(
            "users/me/subscriptions",
            request={
                "subscriptions": [{"name": name}],
                "principals": [credentials[n]["user_id"] for n in clients],
            },
        )
    )
    stream_id = checked(bot.call_endpoint("users/me/subscriptions", method="GET"))["subscriptions"]
    stream_id = next(s["stream_id"] for s in stream_id if s["name"] == name)
    settings = checked(bot.call_endpoint("server_settings", method="GET"))
    members = checked(bot.call_endpoint("users", method="GET"))["members"]
    bots = {str(u["user_id"]) for u in members if u["is_bot"]}
    cfg = Config("TLOCAL", "CLOCAL", credentials["site"], stream_id)
    real = object.__new__(LiveTransport)
    real.cfg, real.zulip = cfg, bot
    real.feature_level = settings["zulip_feature_level"]
    policy = real.read_policy(credentials["bot"]["user_id"])
    bots.update(real.zulip_bots)
    queue = checked(
        bot.register(
            event_types=["message", "update_message", "delete_message", "reaction"],
            apply_markdown=False,
            idle_queue_timeout=604800,
            client_capabilities={
                "notification_settings_null": False,
                "bulk_message_deletion": True,
            },
        )
    )
    real.queue_id = queue["queue_id"]
    cursor = {"queue_id": real.queue_id, "last_event_id": queue["last_event_id"]}
    fake = MemoryTransport()

    class Hybrid:
        max_message_length = real.max_message_length
        max_topic_length = real.max_topic_length
        slack_site = "https://example.slack.com"

        def execute(self, platform, method, args):
            if platform == "zulip":
                return real.execute(platform, method, args)
            return fake.execute(platform, method, args)

    outcomes = []
    echoes = []
    with tempfile.TemporaryDirectory(prefix="bridge-live-") as directory:
        store = Store(Path(directory) / "smoke.sqlite")
        engine = Engine(cfg, store, Hybrid(), "UBOT", str(credentials["bot"]["user_id"]))
        counter = 0

        def send(kind="create", mid=f"{stamp}.000001", **kwargs):
            nonlocal counter
            counter += 1
            store.ingest([Event(f"local:{counter}", "slack", kind, mid, actor="UALICE", **kwargs)])
            assert engine.step()
            assert not store.status()["pending"], store.status()

        def drain():
            # The server queues asynchronously after transactions; poll a bounded period.
            for _ in range(10):
                data = checked(
                    bot.call_endpoint(
                        "events", method="GET", request={**cursor, "dont_block": True}, timeout=10
                    )
                )
                known = {m["zulip"] for m in engine.state["messages"].values()}
                for raw in data["events"]:
                    if raw.get("local_message_id"):
                        echoes.append(raw["local_message_id"])
                    store.ingest(zulip_events(raw, real.queue_id, cfg, bots, known))
                    while engine.step():
                        pass
                    assert not store.status()["pending"], store.status()
                    cursor["last_event_id"] = raw["id"]
                time.sleep(0.2)

        def fetch(mid):
            return checked(
                bot.call_endpoint(
                    f"messages/{mid}", method="GET", request={"apply_markdown": False}
                )
            )["message"]

        send(text="Local Slack simulation: parent", author="Alice", revision="1")
        parent = engine.find("slack", f"{stamp}.000001")["zulip"]
        assert fetch(parent)["subject"] == "Slack feed"
        drain()
        assert echoes, "No local_message_id echo received"
        outcomes += ["Slack-to-real-Zulip send", "own-queue correlation echo"]
        send("edit", text="Local Slack simulation: edited", revision="2")
        assert "edited" in fetch(parent)["content"]
        send("reaction", emoji="thumbsup")
        assert any(r["emoji_code"] == "1f44d" for r in fetch(parent)["reactions"])
        send(mid=f"{stamp}.000002", parent=f"{stamp}.000001", text="Thread reply")
        assert fetch(parent)["subject"] == "Slack feed"
        assert "Discussion continued →" in fetch(parent)["content"]
        parent = engine.find("slack", f"{stamp}.000001")["zulip"]
        assert fetch(parent)["subject"] != "Slack feed"
        drain()
        assert not fake.calls, "System notification was unexpectedly mirrored to Slack"
        outcomes += [
            "edit",
            "reaction",
            "first-reply promotion",
            "continuation notice loop prevention",
        ]
        human = checked(
            alice.call_endpoint(
                "messages",
                request={
                    "type": "stream",
                    "to": stream_id,
                    "topic": f"Zulip topic {stamp}",
                    "content": "From real Alice",
                },
            )
        )
        drain()
        mapped = engine.find("zulip", str(human["id"]))
        assert mapped and "From real Alice" in fake.messages[f"slack:{mapped['slack']}"]["text"]
        checked(
            alice.call_endpoint(
                f"messages/{human['id']}",
                method="PATCH",
                request={"content": "Alice corrected this"},
            )
        )
        drain()
        assert "corrected" in fake.messages[f"slack:{mapped['slack']}"]["text"]
        outcomes += ["real Zulip-to-simulated-Slack send and edit events"]
        checked(alice.call_endpoint(f"messages/{human['id']}", method="DELETE"))
        drain()
        assert f"slack:{mapped['slack']}" not in fake.messages
        send("delete")
        outcomes += ["real Zulip delete event", "bot mirror deletion"]
        # Age only a synthetic message created by this test; leave realm policies intact.
        late_id = f"{stamp}.000010"
        send(mid=late_id, text="Synthetic old copy", author="Alice", revision="1")
        late_zulip = engine.find("slack", late_id)["zulip"]
        age_code = (
            "from datetime import timedelta; from django.utils import timezone; "
            "from zerver.models import Message; "
            "from zerver.lib.message_cache import update_message_cache; "
            f"m=Message.objects.get(id={int(late_zulip)}, "
            f"sender_id={int(credentials['bot']['user_id'])}, "
            f"realm_id={int(credentials['realm_id'])}); "
            "m.date_sent=timezone.now()-timedelta(minutes=15); "
            "m.save(update_fields=['date_sent']); update_message_cache([m])"
        )
        subprocess.run(
            [
                str(ROOT / "control"),
                "exec",
                "-T",
                "-u",
                "zulip",
                "zulip",
                "/home/zulip/deployments/current/manage.py",
                "shell",
                "-c",
                age_code,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        send("edit", mid=late_id, text="Late correction", revision="2")
        assert "Synthetic old copy" in fetch(late_zulip)["content"]
        assert any(i["code"] == "edit_not_applied_notice_posted" for i in engine.state["issues"])
        send("delete", mid=late_id)
        assert "Synthetic old copy" in fetch(late_zulip)["content"]
        assert any(
            i["code"] == "withdrawal_copy_remains_notice_posted" for i in engine.state["issues"]
        )
        outcomes += [
            "late edit notice under real default policy",
            "late delete notice under real default policy",
        ]
        store.close()
    bot.deregister(real.queue_id)
    credentials["channel_id"] = stream_id
    (ROOT / "credentials.json").write_text(json.dumps(credentials, indent=2))
    report = {
        "site": credentials["site"],
        "channel_id": stream_id,
        "zulip_version": settings["zulip_version"],
        "feature_level": settings["zulip_feature_level"],
        "policy": policy,
        "passed": outcomes,
        "slack": "simulated, no live credentials",
        "tested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (ROOT / "test-report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

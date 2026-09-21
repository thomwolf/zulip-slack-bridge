"""Run only inside the pinned disposable Zulip container through manage.py shell."""

import json
import os
import secrets
from pathlib import Path

from zerver.actions.create_realm import do_create_realm
from zerver.actions.create_user import do_create_user
from zerver.models import Realm, UserProfile

os.umask(0o077)
path = Path("/tmp/bridge-credentials.json")
existing = json.loads(path.read_text()) if path.exists() else {}
realm = Realm.objects.filter(string_id="").first()
if realm is None:
    realm = do_create_realm(string_id="", name="Local bridge test", invite_required=True)
if realm.name != "Local bridge test":
    raise RuntimeError("Refusing to provision a different organization")
accounts = {}
for slug, full_name, role in [
    ("admin", "Test administrator", UserProfile.ROLE_REALM_OWNER),
    ("alice", "Alice Test", UserProfile.ROLE_MEMBER),
    ("bob", "Bob Test", UserProfile.ROLE_MEMBER),
]:
    email = f"{slug}@bridge.test"
    user = UserProfile.objects.filter(realm=realm, delivery_email=email).first()
    password = existing.get(slug, {}).get("password") or secrets.token_urlsafe(24)
    if user is None:
        user = do_create_user(
            email,
            password,
            realm,
            full_name,
            role=role,
            realm_creation=slug == "admin",
            acting_user=None,
            enable_marketing_emails=False,
        )
    elif slug not in existing:
        if os.environ.get("BRIDGE_RECOVER_INCOMPLETE_SETUP") != "1":
            raise RuntimeError(
                "Existing account without saved password; do not reset it implicitly"
            )
        user.set_password(password)
        user.save(update_fields=["password"])
    accounts[slug] = {
        "email": user.delivery_email,
        "password": password,
        "api_key": user.api_key,
        "user_id": user.id,
    }
    path.write_text(json.dumps(accounts, indent=2))
alice = UserProfile.objects.get(id=accounts["alice"]["user_id"])
bot = UserProfile.objects.filter(realm=realm, delivery_email="from-slack-bot@bridge.test").first()
if bot is None:
    bot = do_create_user(
        "from-slack-bot@bridge.test",
        None,
        realm,
        "From Slack",
        bot_type=UserProfile.DEFAULT_BOT,
        role=UserProfile.ROLE_MEMBER,
        bot_owner=alice,
        acting_user=alice,
    )
accounts["bot"] = {"email": bot.email, "api_key": bot.api_key, "user_id": bot.id}
accounts["site"] = "https://zulip.localhost:8443"
accounts["realm_id"] = realm.id
path.write_text(json.dumps(accounts, indent=2))
print("Fixture accounts prepared; credentials saved privately inside the container.")

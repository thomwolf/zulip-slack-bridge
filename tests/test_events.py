from chat_bridge.content import emoji_name, neutral, render
from chat_bridge.events import slack_events, zulip_events


def envelope(event, team="T1"):
    return {"team_id": team, "event_id": "Ev1", "event": event}


def test_slack_channel_and_workspace_filter(bridge):
    message = {"type": "message", "channel": "C1", "ts": "1.000001", "user": "UA", "text": "Hi"}
    assert slack_events(envelope(message), bridge.config, {})
    assert not slack_events(envelope(message, "TOTHER"), bridge.config, {})
    assert not slack_events(envelope({**message, "channel": "COTHER"}), bridge.config, {})
    assert not slack_events(envelope({**message, "channel": "D1"}), bridge.config, {})


def test_slack_thread_broadcast_is_one_reply(bridge):
    message = {
        "type": "message",
        "subtype": "thread_broadcast",
        "channel": "C1",
        "ts": "2.000001",
        "thread_ts": "1.000001",
        "user": "UA",
        "text": "Reply",
    }
    events = slack_events(envelope(message), bridge.config, {})
    assert len(events) == 1 and events[0].parent == "1.000001"


def test_slack_reaction_actor_is_not_item_author(bridge):
    event = {
        "type": "reaction_added",
        "user": "UALICE",
        "item_user": "UBOT",
        "reaction": "+1",
        "item": {"type": "message", "channel": "C1", "ts": "1.000001"},
    }
    events = slack_events(envelope(event), bridge.config, {})
    assert events[0].actor == "UALICE" and events[0].emoji == "thumbsup"


def test_slack_message_edit_retains_message_id_and_edit_revision(bridge):
    event = {
        "type": "message",
        "subtype": "message_changed",
        "channel": "C1",
        "message": {
            "ts": "1.000001",
            "user": "UA",
            "text": "Fixed",
            "edited": {"user": "UA", "ts": "2.000001"},
        },
    }
    result = slack_events(envelope(event), bridge.config, {})[0]
    assert result.message_id == "1.000001" and result.revision == "2.000001"


def test_zulip_control_channel_and_bots_are_ignored(bridge):
    raw = {
        "id": 1,
        "type": "message",
        "message": {
            "id": 10,
            "type": "stream",
            "stream_id": 2,
            "sender_id": 7,
            "sender_full_name": "Bob",
            "content": "Hi",
            "subject": "Topic",
        },
    }
    assert not zulip_events(raw, "q", bridge.config, set(), set())
    raw["message"]["stream_id"] = 1
    assert not zulip_events(raw, "q", bridge.config, {"7"}, set())
    assert zulip_events(raw, "q", bridge.config, set(), set())


def test_zulip_move_and_edit_are_separate_and_cross_channel_move_is_flagged(bridge):
    raw = {
        "id": 1,
        "type": "update_message",
        "message_id": 10,
        "message_ids": [10, 11],
        "stream_id": 1,
        "new_stream_id": 2,
        "subject": "Secret",
        "content": "Correction",
        "edit_timestamp": 100,
        "user_id": 7,
    }
    result = zulip_events(raw, "q", bridge.config, set(), {"10", "11"})
    assert [e.kind for e in result] == ["move", "edit"]
    assert result[0].out_of_scope and result[0].ids == ["10", "11"]


def test_zulip_reaction_on_known_bot_message_is_received(bridge):
    raw = {
        "id": 1,
        "type": "reaction",
        "message_id": 10,
        "user_id": 7,
        "emoji_name": "thumbs_up",
        "emoji_code": "1f44d",
        "reaction_type": "unicode_emoji",
        "op": "add",
    }
    result = zulip_events(raw, "q", bridge.config, {"99"}, {"10"})
    assert result[0].actor == "7" and result[0].emoji == "thumbsup"
    assert not zulip_events({**raw, "user_id": 99}, "q", bridge.config, {"99"}, {"10"})
    assert not zulip_events(raw, "q", bridge.config, {"99"}, set())


def test_bulk_delete_only_matches_channel_or_known_messages(bridge):
    raw = {"id": 1, "type": "delete_message", "message_ids": [10, 11, 12]}
    result = zulip_events(raw, "q", bridge.config, set(), {"10", "12"})
    assert [e.message_id for e in result] == ["10", "12"]
    assert len({e.key for e in result}) == 2


def test_rendering_only_edit_is_ignored(bridge):
    raw = {
        "id": 1,
        "type": "update_message",
        "message_id": 10,
        "stream_id": 1,
        "content": "Unfurl changed",
        "user_id": None,
        "rendering_only": True,
    }
    assert not zulip_events(raw, "q", bridge.config, set(), {"10"})


def test_queue_event_ids_are_namespaced_by_queue(bridge):
    raw = {"id": 1, "type": "delete_message", "message_ids": [10], "stream_id": 1}
    a = zulip_events(raw, "q1", bridge.config, set(), set())[0]
    b = zulip_events(raw, "q2", bridge.config, set(), set())[0]
    assert a.key != b.key


def test_mentions_and_display_name_injection_are_neutralized():
    text = render(
        "Alice\n@**everyone**",
        "slack",
        "<!channel> <@U1> <https://example.com|Link>",
        "https://example.com",
    )
    assert "<!channel>" not in text and "<@U1>" not in text
    assert "@**" not in neutral("@**all**")
    assert "[Link](https://example.com)" in text
    assert "\n" not in text.split(" · Slack")[0]


def test_custom_emoji_is_ignored_and_skin_tones_reduce_to_base():
    assert not emoji_name("heart", "123", "realm_emoji")
    assert emoji_name("thumbsup::skin-tone-4") == "thumbsup"
    assert emoji_name("+1") == "thumbsup"


def test_system_bots_from_registration_snapshot_never_mirror_notifications(bridge):
    from chat_bridge.events import zulip_bot_ids

    ids = zulip_bot_ids(
        {
            "realm_users": [{"user_id": 99, "is_bot": True}, {"user_id": 7, "is_bot": False}],
            "cross_realm_bots": [{"user_id": 42, "is_bot": True}],
        }
    )
    raw = {
        "id": 1,
        "type": "message",
        "message": {
            "id": 100,
            "type": "stream",
            "stream_id": 1,
            "sender_id": 42,
            "content": "Message moved",
            "subject": "Slack feed",
            "sender_full_name": "Notification Bot",
        },
    }
    assert ids == {"99", "42"}
    assert not zulip_events(raw, "q", bridge.config, ids, set())

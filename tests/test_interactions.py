import pytest


@pytest.mark.parametrize(
    "origin,mid,actor,topic",
    [
        ("slack", "1.000001", "UA", ""),
        ("zulip", "1", "1", "Slack feed"),
    ],
)
def test_edits_update_same_mirror_without_losing_reactions(bridge, origin, mid, actor, topic):
    bridge.send(origin, mid=mid, actor=actor, author="Alice", text="Old", topic=topic, revision="1")
    m = bridge.engine.find(origin, mid)
    destination = "zulip" if origin == "slack" else "slack"
    bridge.send(origin, "reaction", mid, actor="someone", emoji="thumbsup")
    bridge.send(origin, "edit", mid, actor=actor, text="New", revision="2")
    mirror = bridge.transport.messages[f"{destination}:{m[destination]}"]
    assert "New" in mirror["text"] and "Old" not in mirror["text"]
    assert mirror["reactions"] == ["thumbsup"]
    bridge.send(origin, "edit", mid, actor=actor, text="Obsolete", revision="1")
    assert "Obsolete" not in mirror["text"]


def test_multiple_people_share_one_remote_reaction(bridge):
    bridge.send(actor="UA", text="Hi")
    for actor in ("UB", "UC"):
        bridge.send(kind="reaction", actor=actor, emoji="thumbsup")
    bridge.send(kind="reaction", actor="UB", emoji="thumbsup", added=False)
    calls = [args for _, method, args in bridge.transport.calls if method == "react"]
    assert len(calls) == 1 and calls[0]["added"]
    bridge.restart()
    bridge.send(kind="reaction", actor="UC", emoji="thumbsup", added=False)
    assert bridge.transport.calls[-1][2]["added"] is False


def test_reactions_on_bot_copy_mirror_back_without_echo(bridge):
    bridge.send(actor="UA", text="Hi")
    mid = bridge.engine.find("slack", "1.000001")["zulip"]
    bridge.send("zulip", "reaction", mid, actor="7", emoji="heart")
    assert bridge.transport.calls[-1][:2] == ("slack", "react")
    count = len(bridge.transport.calls)
    bridge.send(kind="reaction", actor="UBOT", emoji="heart")
    bridge.send("zulip", "reaction", mid, actor="99", emoji="heart")
    assert len(bridge.transport.calls) == count


def test_source_deletion_removes_mirror_and_never_resurrects(bridge):
    bridge.send(actor="UA", text="Withdraw me")
    mid = bridge.engine.find("slack", "1.000001")["zulip"]
    bridge.send(kind="delete")
    bridge.send(kind="edit", actor="UA", text="Late edit", revision="9")
    assert f"zulip:{mid}" not in bridge.transport.messages
    assert bridge.engine.find("slack", "1.000001")["text"] == ""


def test_destination_moderation_does_not_delete_original(bridge):
    bridge.send(actor="UA", text="Hi")
    mid = bridge.engine.find("slack", "1.000001")["zulip"]
    count = len(bridge.transport.calls)
    bridge.send("zulip", "delete", mid)
    bridge.send(kind="edit", actor="UA", text="New", revision="2")
    assert len(bridge.transport.calls) == count
    assert bridge.engine.find("slack", "1.000001")["suppressed"]


def test_delete_before_create_does_not_publish_withdrawn_text(bridge):
    bridge.send(kind="delete")
    bridge.send(actor="UA", text="Never show this")
    assert not bridge.transport.calls


def test_edit_before_create_is_applied_after_mapping_exists(bridge):
    bridge.send(kind="edit", actor="UA", text="Correction", revision="2")
    bridge.send(actor="UA", text="Old", revision="1")
    assert "Correction" in bridge.transport.messages["zulip:101"]["text"]


def test_parent_deletion_does_not_cascade_to_replies(bridge):
    bridge.send(actor="UA", text="Parent")
    bridge.send(mid="2.000001", actor="UB", text="Reply", parent="1.000001")
    reply = bridge.engine.find("slack", "2.000001")["zulip"]
    bridge.send(kind="delete")
    assert f"zulip:{reply}" in bridge.transport.messages


def test_zulip_same_second_edit_is_not_lost(bridge):
    bridge.send("zulip", mid="1", actor="7", topic="Slack feed", text="Old", revision="100")
    bridge.send("zulip", "edit", "1", actor="7", text="Fixed", revision="100")
    assert "Fixed" in bridge.transport.messages["slack:101.000001"]["text"]


def test_move_out_of_channel_stops_future_content_export(bridge):
    bridge.send("zulip", mid="1", actor="7", topic="Public", text="Public text")
    bridge.send("zulip", "move", "1", ids=["1"], topic="Private", out_of_scope=True)
    count = len(bridge.transport.calls)
    bridge.send("zulip", "edit", "1", actor="7", text="Private correction", revision="5")
    assert len(bridge.transport.calls) == count


def test_old_bot_edit_echo_is_not_mistaken_for_local_moderation(bridge):
    bridge.send(actor="UA", text="Old", revision="1")
    mid = bridge.engine.find("slack", "1.000001")["zulip"]
    original_copy = bridge.transport.messages[f"zulip:{mid}"]["text"]
    bridge.send(kind="edit", actor="UA", text="New", revision="2")
    bridge.send("zulip", "edit", mid, actor="99", text=original_copy, revision="3")
    assert not bridge.engine.find("slack", "1.000001")["suppressed"]


def test_unexpected_edit_using_bot_credentials_is_detected(bridge):
    bridge.send(actor="UA", text="Original")
    mid = bridge.engine.find("slack", "1.000001")["zulip"]
    bridge.send("zulip", "edit", mid, actor="99", text="Moderated", revision="2")
    assert bridge.engine.find("slack", "1.000001")["suppressed"]

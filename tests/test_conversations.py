from chat_bridge.engine import initial_state
from chat_bridge.model import DeliveryError, Event


def test_standalone_messages_stay_in_feed_and_duplicates_are_not_resent(bridge):
    for i in range(20):
        event = bridge.send(mid=f"{i}.000001", actor="UA", author="Alice", text="Hello")
        bridge.store.ingest([event])
        assert not bridge.engine.step()
    sends = [args for _, method, args in bridge.transport.calls if method == "send"]
    assert len(sends) == 20
    assert {s["topic"] for s in sends} == {"Slack feed"}
    assert all(not c["topic"] for c in bridge.engine.state["conversations"].values())


def test_first_reply_leaves_parent_and_adds_topic_link_once(bridge):
    bridge.send(actor="UA", author="Alice", text="Question", revision="1")
    parent = bridge.engine.find("slack", "1.000001")["zulip"]
    bridge.send(mid="2.000001", actor="UB", text="Answer", parent="1.000001")
    bridge.send(mid="3.000001", actor="UC", text="Another answer", parent="1.000001")
    assert not [c for c in bridge.transport.calls if c[1] == "move"]
    original = bridge.transport.messages[f"zulip:{parent}"]
    assert original["topic"] == "Slack feed"
    assert "Question" in original["text"] and "Discussion continued →" in original["text"]
    contexts = [
        a
        for _, method, a in bridge.transport.calls
        if method == "send" and a["text"].endswith("\nQuestion") and a["topic"] != "Slack feed"
    ]
    assert len(contexts) == 1
    assert contexts[0]["topic"] != "Slack feed"
    bridge.send(kind="edit", text="Revised question", revision="2")
    current = bridge.engine.find("slack", "1.000001")["zulip"]
    assert current != parent
    assert "Revised question" in bridge.transport.messages[f"zulip:{current}"]["text"]
    assert "Revised question" not in original["text"]


def test_permission_denial_leaves_original_and_posts_feed_link(bridge):
    execute = bridge.transport.execute

    def deny_edit(platform, method, args):
        if method == "edit":
            raise DeliveryError("too_old", "denied")
        return execute(platform, method, args)

    bridge.transport.execute = deny_edit
    bridge.send(text="Sensitive original text", actor="UA")
    bridge.send(mid="2.000001", text="Reply", actor="UB", parent="1.000001")
    texts = [args["text"] for _, method, args in bridge.transport.calls if method == "send"]
    assert sum(t.endswith("\nSensitive original text") for t in texts) == 2
    notices = [
        args
        for _, method, args in bridge.transport.calls
        if method == "send" and "Discussion continued →" in args["text"]
    ]
    assert len(notices) == 1 and notices[0]["topic"] == "Slack feed"
    assert bridge.engine.state["conversations"]["1.000001"]["fallback"]


def test_promotion_retry_reuses_context_and_keeps_replies_in_topic(bridge):
    bridge.send(actor="UA", text="Question")
    execute = bridge.transport.execute

    def pause_link(platform, method, args):
        if method == "edit":
            raise DeliveryError("rate_limit", "retry")
        return execute(platform, method, args)

    bridge.transport.execute = pause_link
    event = bridge.send(mid="2.000001", actor="UB", text="Answer", parent="1.000001")
    assert bridge.store.status()["pending"]
    bridge.restart()
    bridge.transport.execute = execute
    bridge.store.retry(event.key)
    assert bridge.engine.step()
    contexts = [
        a
        for _, method, a in bridge.transport.calls
        if method == "send" and a["text"].endswith("\nQuestion") and a["topic"] != "Slack feed"
    ]
    assert len(contexts) == 1
    topic = bridge.engine.state["conversations"]["1.000001"]["topic"]
    assert bridge.transport.calls[-1][2]["topic"] == topic
    bridge.send("zulip", mid="900", actor="1", text="Return reply", topic=topic)
    assert bridge.transport.calls[-1][2]["parent"] == "1.000001"


def test_zulip_topic_creates_parent_then_slack_replies(bridge):
    bridge.send("zulip", mid="10", actor="1", text="First", topic="Research")
    root = bridge.engine.find("zulip", "10")["root"]
    bridge.send("zulip", mid="11", actor="2", text="Second", topic="Research")
    assert bridge.transport.calls[-1][2]["parent"] == root
    bridge.send("slack", mid="3.000001", actor="UA", text="Third", parent=root)
    assert bridge.transport.calls[-1][2]["topic"] == "Research"


def test_promoted_copy_receives_reactions_and_deletion_not_feed_pointer(bridge):
    bridge.send(actor="UA", text="Question")
    old = bridge.engine.find("slack", "1.000001")["zulip"]
    bridge.send(kind="reaction", actor="UB", emoji="thumbsup")
    bridge.send(mid="2.000001", actor="UB", text="Answer", parent="1.000001")
    new = bridge.engine.find("slack", "1.000001")["zulip"]
    assert new != old
    assert bridge.transport.messages[f"zulip:{new}"]["reactions"] == ["thumbsup"]
    bridge.send(kind="reaction", actor="UB", emoji="thumbsup", added=False)
    assert bridge.transport.messages[f"zulip:{new}"]["reactions"] == []
    bridge.send("zulip", "edit", mid=old, text="Pointer echo")
    assert not bridge.engine.find("slack", "1.000001")["suppressed"]
    bridge.send(kind="delete")
    assert f"zulip:{new}" not in bridge.transport.messages
    assert f"zulip:{old}" in bridge.transport.messages


def test_zulip_authored_feed_original_and_topic_copy_stay_in_sync(bridge):
    bridge.send("zulip", mid="10", actor="1", author="Alice", text="Original", topic="Slack feed")
    root = bridge.engine.find("zulip", "10")["root"]
    bridge.send(mid="2.000001", actor="UA", text="Reply", parent=root)
    copy = bridge.engine.find("zulip", "10")["topic_copy"]
    assert bridge.transport.messages[f"zulip:{copy}"]["text"].endswith("\nOriginal")
    bridge.send("zulip", "edit", mid="10", actor="1", text="Corrected", revision="2")
    assert bridge.transport.messages[f"zulip:{copy}"]["text"].endswith("\nCorrected")
    bridge.send("zulip", "delete", mid="10", actor="1")
    assert f"zulip:{copy}" not in bridge.transport.messages
    assert f"slack:{root}" not in bridge.transport.messages


def test_reactions_on_feed_and_topic_copies_keep_distinct_memberships(bridge):
    bridge.send(actor="UA", text="Question")
    old = bridge.engine.find("slack", "1.000001")["zulip"]
    bridge.send("zulip", "reaction", mid=old, actor="7", emoji="thumbsup")
    bridge.send(mid="2.000001", actor="UB", text="Reply", parent="1.000001")
    new = bridge.engine.find("slack", "1.000001")["zulip"]
    assert bridge.transport.messages[f"zulip:{new}"]["reactions"] == ["thumbsup"]
    bridge.send("zulip", "reaction", mid=new, actor="7", emoji="thumbsup")
    bridge.send("zulip", "reaction", mid=old, actor="7", emoji="thumbsup", added=False)
    assert bridge.transport.messages["slack:1.000001"]["reactions"] == ["thumbsup"]
    bridge.send("zulip", "reaction", mid=new, actor="7", emoji="thumbsup", added=False)
    assert bridge.transport.messages["slack:1.000001"]["reactions"] == []


def test_promoted_topic_is_not_repeated_in_reply_attribution_or_edits(bridge):
    bridge.send("zulip", mid="10", actor="1", author="Alice", text="Hello", topic="Slack feed")
    root = bridge.engine.find("zulip", "10")["root"]
    bridge.send(mid="8.000001", actor="UA", text="Answer", parent=root)
    topic = bridge.engine.state["conversations"][root]["topic"]
    bridge.send("zulip", mid="11", actor="1", author="Alice", text="Good to see you", topic=topic)
    assert (
        bridge.transport.calls[-1][2]["text"].splitlines()[0]
        == "<https://zulip.test/#narrow/id/11|Alice> · Zulip"
    )
    bridge.send("zulip", "edit", mid="11", text="Great to see you", revision="2")
    assert (
        bridge.transport.calls[-1][2]["text"].splitlines()[0]
        == "<https://zulip.test/#narrow/id/11|Alice> · Zulip"
    )


def test_native_topic_title_has_separate_parent_and_first_reply(bridge):
    bridge.send("zulip", mid="10", actor="1", author="Alice", text="First", topic="Research")
    assert (
        bridge.transport.calls[-1][2]["text"].splitlines()[0]
        == "<https://zulip.test/#narrow/id/10|Alice> · Zulip"
    )
    heading, reply = bridge.transport.calls[:2]
    assert "|Research>" in heading[2]["text"] and not heading[2]["parent"]
    assert reply[2]["parent"] == bridge.engine.find("zulip", "10")["root"]
    assert bridge.engine.find("zulip", "10")["slack"] != reply[2]["parent"]
    bridge.send("zulip", mid="11", actor="1", author="Alice", text="Second", topic="Research")
    assert (
        bridge.transport.calls[-1][2]["text"].splitlines()[0]
        == "<https://zulip.test/#narrow/id/11|Alice> · Zulip"
    )


def test_first_zulip_message_interactions_target_reply_not_heading(bridge):
    bridge.send("zulip", mid="10", actor="1", text="First", topic="Research", revision="1")
    message = bridge.engine.find("zulip", "10")
    root, reply = message["root"], message["slack"]
    heading = dict(bridge.transport.messages[f"slack:{root}"])
    bridge.send("zulip", "edit", mid="10", actor="1", text="Updated", revision="2")
    assert bridge.transport.calls[-1][2]["id"] == reply
    bridge.send("zulip", "reaction", mid="10", actor="2", emoji="thumbsup")
    assert bridge.transport.calls[-1][2]["id"] == reply
    bridge.send("zulip", "delete", mid="10", actor="1")
    assert f"slack:{reply}" not in bridge.transport.messages
    assert bridge.transport.messages[f"slack:{root}"] == heading


def test_restart_between_heading_and_reply_does_not_duplicate_heading(bridge):
    execute = bridge.transport.execute

    def pause_reply(platform, method, args):
        if method == "send" and args.get("parent"):
            raise DeliveryError("rate_limit", "retry")
        return execute(platform, method, args)

    bridge.transport.execute = pause_reply
    event = bridge.send("zulip", mid="10", actor="1", text="First", topic="Research")
    assert bridge.store.status()["pending"]
    bridge.restart()
    bridge.transport.execute = execute
    bridge.store.retry(event.key)
    assert bridge.engine.step()
    assert len(bridge.transport.calls) == 2
    message = bridge.engine.find("zulip", "10")
    assert bridge.transport.calls[-1][2]["parent"] == message["root"]


def test_heading_echo_and_reactions_do_not_change_first_message(bridge):
    bridge.send("zulip", mid="10", actor="1", text="First", topic="Research")
    root = bridge.engine.find("zulip", "10")["root"]
    heading = bridge.engine.state["conversations"][root]["heading"]
    bridge.send("slack", "edit", mid=root, text=heading.strip())
    bridge.send("slack", "reaction", mid=root, actor="UA", emoji="thumbsup")
    assert len(bridge.transport.calls) == 2
    assert not bridge.engine.state["conversations"][root]["blocked"]


def test_zulip_feed_parent_promotes_when_slack_user_replies(bridge):
    bridge.send("zulip", mid="10", actor="1", text="Hello", topic="Slack feed")
    root = bridge.engine.find("zulip", "10")["root"]
    bridge.send(mid="8.000001", actor="UA", text="Answer", parent=root)
    assert not [c for c in bridge.transport.calls if c[1] in {"move", "edit"}]
    notices = [
        a
        for _, method, a in bridge.transport.calls
        if method == "send" and "Discussion continued →" in a["text"]
    ]
    assert len(notices) == 1 and notices[0]["topic"] == "Slack feed"


def test_moving_feed_message_binds_existing_parent(bridge):
    bridge.send("zulip", mid="10", actor="1", text="Hi", topic="Slack feed")
    root = bridge.engine.find("zulip", "10")["root"]
    bridge.send("zulip", "move", "10", ids=["10"], topic="A topic", actor="1")
    bridge.send("zulip", mid="11", actor="1", text="Follow up", topic="A topic")
    assert bridge.transport.calls[-1][2]["parent"] == root


def test_topic_rename_survives_restart(bridge):
    bridge.send("zulip", mid="10", actor="1", text="Hi", topic="Old")
    root = bridge.engine.find("zulip", "10")["root"]
    bridge.send("zulip", "move", "10", ids=["10"], topic="New", actor="1")
    bridge.restart()
    bridge.send("zulip", mid="11", actor="2", text="Hi again", topic="New")
    assert bridge.transport.calls[-1][2]["parent"] == root


def test_merge_does_not_guess_a_thread(bridge):
    bridge.send("zulip", mid="10", actor="1", text="First", topic="A")
    bridge.send("zulip", mid="11", actor="1", text="Second", topic="B")
    bridge.send("zulip", "move", "10", ids=["10"], topic="B", actor="1")
    before = len(bridge.transport.calls)
    bridge.send("zulip", mid="12", actor="1", text="Where?", topic="B")
    assert len(bridge.transport.calls) == before
    assert bridge.store.status()["pending"][0]["status"] == "failed"


def test_unknown_parent_is_held_not_flattened(bridge):
    bridge.send(mid="2.000001", actor="UA", text="Reply", parent="missing")
    assert not bridge.transport.calls
    assert bridge.store.status()["pending"][0]["error"] == "missing_thread_parent"


def test_bot_messages_do_not_echo(bridge):
    bridge.send(actor="UBOT", text="Bot copy")
    bridge.send("zulip", mid="100", actor="99", text="Breadcrumb", topic="Slack feed")
    assert not bridge.transport.calls


def test_empty_state_is_not_shared_between_instances():
    first = initial_state()
    first["messages"]["foo"] = 1
    assert not initial_state()["messages"]


def test_distinct_repeated_text_is_not_deduplicated(bridge):
    bridge.store.ingest(
        [
            Event("one", "slack", "create", "1.000001", actor="UA", text="Same"),
            Event("two", "slack", "create", "2.000001", actor="UA", text="Same"),
        ]
    )
    assert bridge.engine.step() and bridge.engine.step()
    assert len(bridge.transport.messages) == 2

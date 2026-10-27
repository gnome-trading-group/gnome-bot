import json

import pytest

from bot import agent, fixes, memory, slack
from bot.handlers import interact, worker
from tests.conftest import BOT_USER


@pytest.fixture
def config_with(monkeypatch, bot_config):
    monkeypatch.setattr(worker.runtime_config, "refresh", lambda: None)
    monkeypatch.setattr(worker.runtime_config, "_config", bot_config)
    return bot_config


@pytest.fixture
def emails(monkeypatch):
    monkeypatch.setattr(slack, "user_email", lambda user_id: f"{user_id.lower()}@gnometrading.group")
    monkeypatch.setattr(slack, "permalink", lambda channel, ts: f"https://slack/{channel}/{ts}")


def answering(monkeypatch, reply):
    asked = []
    monkeypatch.setattr(agent, "run", lambda cfg, role, q, tools, extra=None: asked.append((role, extra, tools))
                        or agent.Answer(reply, 10, "anthropic"))
    return asked


def question(text, thread_ts="100.1"):
    return {"kind": "question", "channel": "C1", "thread_ts": thread_ts, "ts": thread_ts, "user": "U1",
            "text": f"<@{BOT_USER}> {text}", "bot_user_id": BOT_USER}


def ask(fake_slack, store, text):
    fake_slack.threads[("C1", "100.1")] = [{"ts": "100.1", "user": "U1", "text": f"<@{BOT_USER}> {text}"}]
    worker.answer_question(question(text), store)


def click(store, action_id, note_id, message, typed=None):
    interact._store = store
    state = {"values": {memory.NOTE_BLOCK_ID: {memory.NOTE_INPUT_ID: {"type": "plain_text_input", "value": typed}}}}
    interact.handler({"user": {"id": "U2"}, "channel": {"id": "C1"}, "message": message,
                      **({"state": state} if typed is not None else {}),
                      "actions": [{"action_id": action_id, "value": note_id}]}, None)


def save(store, note_id="m-abc123", scope="general", note="Shard resizes double EC2 spend for a day.",
         saved_at="2026-10-27"):
    store.save_memory(note_id, {"scope": scope, "note": note, "channel": "C1", "thread_ts": "1.1",
                                "saved_by": "u1@gnometrading.group", "saved_at": saved_at})


@pytest.mark.parametrize("answer,expected", [
    ("Done.\nRemember (COST_ANOMALY): NAT spend tracks collector shards.",
     ("Done.", ("COST_ANOMALY", "NAT spend tracks collector shards."))),
    ("Done.\nRemember: Paper PnL resets at midnight ET.", ("Done.", ("general", "Paper PnL resets at midnight ET."))),
    ("Done.\nRemember (not a scope!): x", ("Done.", ("general", "x"))),
    ("Remember (general): a\nRepo: gnome-registry", ("Repo: gnome-registry", ("general", "a"))),
    ("Nothing to remember here.", ("Nothing to remember here.", None)),
])
def test_splits_a_proposed_note_off_the_reply(answer, expected):
    assert memory.split_memory_line(answer) == expected


def test_a_proposed_note_is_offered_with_save_and_dismiss_but_not_kept(store, fake_slack, config_with, monkeypatch):
    answering(monkeypatch, "Got it, that's expected after a resize.\nRemember (COST_ANOMALY): Shard resizes double "
                           "EC2 spend for a day.")
    ask(fake_slack, store, "that cost anomaly is just the shard resize")

    update = fake_slack.updates[0]
    assert update["text"] == "Got it, that's expected after a resize."
    offer, buttons = update["blocks"][-2:]
    assert offer["type"] == "input" and offer["element"]["initial_value"].startswith("Shard resizes double EC2 spend")
    assert [b["action_id"] for b in buttons["elements"]] == [memory.SAVE_ACTION_ID, memory.DISMISS_ACTION_ID]
    assert store.memories() == {}
    assert store.memory_proposal(buttons["elements"][0]["value"])["scope"] == "COST_ANOMALY"


def test_save_keeps_the_note_and_replaces_the_buttons(store, fake_slack, emails):
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "answer"}}, *memory.propose(
        store, "C1", "100.1", "COST_ANOMALY", "Shard resizes double EC2 spend for a day.")]
    note_id = blocks[-1]["elements"][0]["value"]
    click(store, memory.SAVE_ACTION_ID, note_id, {"ts": "100.2", "text": "answer", "blocks": blocks})

    saved = store.memories()[note_id]
    assert saved["scope"] == "COST_ANOMALY" and saved["saved_by"] == "u2@gnometrading.group"
    assert saved["note"] == "Shard resizes double EC2 spend for a day." and saved["link"] == "https://slack/C1/100.1"
    assert store.memory_proposal(note_id) is None
    update = fake_slack.updates[0]
    assert update["ts"] == "100.2" and [b["type"] for b in update["blocks"]] == ["section", "context"]
    assert f"Saved as `{note_id}`" in update["blocks"][-1]["elements"][0]["text"]


def test_saves_the_wording_as_edited_in_the_message(store, fake_slack, emails):
    blocks = memory.propose(store, "C1", "100.1", "general", "Paper pnl resets.")
    note_id = blocks[-1]["elements"][0]["value"]
    click(store, memory.SAVE_ACTION_ID, note_id, {"ts": "100.2", "text": "a", "blocks": blocks},
          typed="Paper PnL resets at\nmidnight ET.")
    assert store.memories()[note_id]["note"] == "Paper PnL resets at midnight ET."


def test_an_emptied_note_isnt_saved(store, fake_slack, emails):
    blocks = memory.propose(store, "C1", "100.1", "general", "x")
    note_id = blocks[-1]["elements"][0]["value"]
    click(store, memory.SAVE_ACTION_ID, note_id, {"ts": "100.2", "text": "a", "blocks": blocks}, typed="   ")
    assert store.memories() == {} and store.memory_proposal(note_id) is not None
    assert "empty" in fake_slack.posts[0]["text"]


def test_dismiss_drops_the_proposal(store, fake_slack):
    blocks = memory.propose(store, "C1", "100.1", "general", "x")
    note_id = blocks[-1]["elements"][0]["value"]
    click(store, memory.DISMISS_ACTION_ID, note_id, {"ts": "100.2", "text": "answer", "blocks": blocks})
    assert store.memories() == {} and store.memory_proposal(note_id) is None
    assert "dismissed" in fake_slack.updates[0]["blocks"][-1]["elements"][0]["text"]


def test_an_expired_proposal_isnt_saved(store, fake_slack):
    click(store, memory.SAVE_ACTION_ID, "m-ffffff", {"ts": "100.2", "text": "answer", "blocks": []})
    assert store.memories() == {} and "expired" in fake_slack.updates[0]["blocks"][-1]["elements"][0]["text"]


def test_triage_sees_its_alert_types_notes_and_the_general_ones(store, fake_slack, config_with, monkeypatch):
    save(store, "m-000001", "COST_ANOMALY", "Shard resizes double EC2 spend for a day.")
    save(store, "m-000002", "general", "Paper sessions never page.")
    save(store, "m-000003", "RISK_HALT", "Halts need a person.")
    config_with.feature_flags.triage_enabled = True
    asked = answering(monkeypatch, "*Likely cause:* the resize.")
    worker.triage_alert({"type": "COST_ANOMALY", "severity": "ERROR", "key": "cost-anomaly:1", "title": "EC2 up",
                         "slackChannel": "CALERTS", "slackTs": "500.1"}, store)

    role, extra, tools = asked[0]
    assert "Shard resizes double EC2" in extra and "Paper sessions never page" in extra
    assert "Halts need a person" not in extra and "RISK_HALT" in extra
    assert memory.team_notes in tools


def test_notes_past_the_prompt_budget_are_left_out_newest_kept(store, monkeypatch):
    monkeypatch.setattr(memory, "MAX_PROMPT_CHARS", 200)
    save(store, "m-000001", note="old " * 30, saved_at="2026-01-01")
    save(store, "m-000002", note="new " * 30, saved_at="2026-10-27")
    section = memory.prompt_section(store, ["general"])
    assert "m-000002" in section and "m-000001" not in section


def test_no_notes_means_no_section(store):
    assert memory.prompt_section(store, ["general"]) is None


def test_forget_deletes_a_note(store, fake_slack, config_with, monkeypatch):
    save(store)
    asked = answering(monkeypatch, "unused")
    ask(fake_slack, store, "forget m-abc123")
    assert store.memories() == {} and asked == []
    assert fake_slack.posts[0]["text"].startswith("Forgot `m-abc123`")


def test_forgetting_an_unknown_note_says_so(store, fake_slack, config_with):
    ask(fake_slack, store, "forget `m-999999`")
    assert fake_slack.posts[0]["text"] == "I have no note `m-999999`."


def test_promote_starts_a_fix_that_moves_the_note_into_the_repos_docs(store, fake_slack, config_with, monkeypatch):
    save(store, scope="COST_ANOMALY")
    config_with.feature_flags.fix_enabled = True
    dispatched = []
    monkeypatch.setattr(fixes.github, "dispatch_workflow", lambda wf, inputs: dispatched.append(inputs) or "https://run")
    monkeypatch.setattr(slack, "user_email", lambda user_id: "u1@gnometrading.group")
    ask(fake_slack, store, "promote m-abc123 to gnome-market-data")

    inputs = dispatched[0]
    assert inputs["repos"] == "gnome-market-data"
    assert "Shard resizes double EC2" in inputs["task"] and "forget m-abc123" in inputs["task"]
    assert "m-abc123" in store.memories()


def test_team_notes_tool_lists_notes_with_their_ids(store, monkeypatch):
    monkeypatch.setattr(memory, "_store", store)
    save(store, "m-000001", "COST_ANOMALY")
    save(store, "m-000002", "general")
    assert [n["id"] for n in json.loads(memory.team_notes.call({}))] == ["m-000001", "m-000002"]
    assert [n["id"] for n in json.loads(memory.team_notes.call({"scope": "COST_ANOMALY"}))] == ["m-000001"]

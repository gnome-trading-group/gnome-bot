import json

import pytest

from bot import agent, alerts, llm, memory, slack
from bot.handlers import worker
from tests.conftest import BOT_USER


@pytest.fixture
def config_with(monkeypatch, bot_config):
    monkeypatch.setattr(worker.runtime_config, "refresh", lambda: None)
    monkeypatch.setattr(worker.runtime_config, "_config", bot_config)
    return bot_config


@pytest.fixture
def sent_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "once_a_day", lambda store, key, title, body: sent.append(key))
    return sent


def question(ts="100.1", thread_ts="100.1", text="what's pnl today?"):
    return {"kind": "question", "channel": "C1", "thread_ts": thread_ts, "ts": ts, "user": "U1", "text": text,
            "bot_user_id": BOT_USER}


def seed_thread(fake_slack, *messages):
    fake_slack.threads[("C1", "100.1")] = list(messages)


def test_reacts_while_working_then_answers_with_a_new_reply(store, fake_slack, config_with, monkeypatch):
    seed_thread(fake_slack, {"ts": "100.1", "user": "U1", "text": f"<@{BOT_USER}> what's pnl today?"})
    asked = []
    monkeypatch.setattr(agent, "run", lambda cfg, role, q, tools, extra=None: asked.append((role, q)) or agent.Answer("Up $12.", 900, "anthropic"))

    worker.answer_question(question(), store)

    assert fake_slack.reactions == [("add", "C1", "100.1", "eyes"), ("remove", "C1", "100.1", "eyes")]
    assert [(p["thread_ts"], p["text"]) for p in fake_slack.posts] == [("100.1", "Up $12.")]
    assert fake_slack.updates == []
    assert asked[0][0] == "qa" and asked[0][1].endswith("from U1:\nwhat's pnl today?")
    assert store.tokens_today() == 900
    assert store.is_tracked_thread("C1", "100.1")


def test_says_so_when_questions_are_switched_off(store, fake_slack, config_with):
    config_with.feature_flags.qa_enabled = False
    worker.answer_question(question(), store)
    assert "qa_enabled" in fake_slack.posts[0]["text"] and fake_slack.reactions == []
    assert not store.is_tracked_thread("C1", "100.1")


def test_stops_at_the_daily_token_budget(store, fake_slack, config_with, sent_alerts, monkeypatch):
    config_with.limits.daily_token_budget = 1000
    store.add_tokens(1000)
    monkeypatch.setattr(agent, "run", lambda *a: pytest.fail("ran over budget"))
    worker.answer_question(question(), store)
    assert "token budget" in fake_slack.posts[0]["text"] and sent_alerts == ["over-budget"]
    assert fake_slack.reactions[-1][0] == "remove"


def test_reports_an_unreachable_model_once_and_answers_the_thread(store, fake_slack, config_with, sent_alerts,
                                                                   monkeypatch):
    seed_thread(fake_slack, {"ts": "100.1", "user": "U1", "text": "pnl?"})

    def unavailable(*_):
        raise llm.ModelUnavailable("Your credit balance is too low")
    monkeypatch.setattr(agent, "run", unavailable)

    worker.answer_question(question(), store)
    assert "can't reach my model" in fake_slack.posts[0]["text"] and sent_alerts == ["model-unavailable"]


def test_answers_an_unexpected_failure_instead_of_raising_into_a_retry(store, fake_slack, config_with, monkeypatch):
    seed_thread(fake_slack, {"ts": "100.1", "user": "U1", "text": "pnl?"})
    monkeypatch.setattr(agent, "run", lambda *a: 1 / 0)
    worker.answer_question(question(), store)
    assert "went wrong" in fake_slack.posts[0]["text"] and fake_slack.reactions[-1][0] == "remove"


def test_asks_to_be_invited_when_it_cant_read_the_channel(store, fake_slack, config_with, monkeypatch):
    def not_a_member(*args, **kwargs):
        raise slack.SlackError("Slack API error (conversations.replies): not_in_channel")
    monkeypatch.setattr(slack, "thread_messages", not_a_member)
    worker.answer_question(question(), store)
    assert "/invite @Gnome Bot" in fake_slack.posts[0]["text"]


def test_a_message_it_cant_react_to_is_dropped_without_raising(store, fake_slack, config_with, monkeypatch):
    def read_only(*args):
        raise slack.SlackError("Slack API error (reactions.add): restricted_action_read_only_channel")
    monkeypatch.setattr(slack, "add_reaction", read_only)
    monkeypatch.setattr(agent, "run", lambda *a: pytest.fail("answered a message it can't reply to"))
    worker.answer_question(question(), store)
    assert fake_slack.posts == []


def test_a_channel_it_isnt_in_gets_the_invite_hint_without_an_answer(store, fake_slack, config_with, monkeypatch):
    def not_a_member(*args):
        raise slack.SlackError("Slack API error (reactions.add): not_in_channel")
    monkeypatch.setattr(slack, "add_reaction", not_a_member)
    monkeypatch.setattr(agent, "run", lambda *a: pytest.fail("answered without reading the channel"))
    worker.answer_question(question(), store)
    assert "/invite @Gnome Bot" in fake_slack.posts[0]["text"]


def test_a_reply_slack_refuses_is_logged_not_raised(store, fake_slack, config_with, monkeypatch):
    seed_thread(fake_slack, {"ts": "100.1", "user": "U1", "text": "pnl?"})
    monkeypatch.setattr(agent, "run", lambda *a: agent.Answer("Up $12.", 10, "anthropic"))

    def refuse(*args, **kwargs):
        raise slack.SlackError("Slack API error (chat.postMessage): is_archived")
    monkeypatch.setattr(slack, "post", refuse)
    worker.answer_question(question(), store)
    assert fake_slack.reactions[-1][0] == "remove"


def test_the_question_carries_the_thread_with_names(fake_slack):
    fake_slack.names = {"U1": "alice", "U2": "bob"}
    seed_thread(
        fake_slack,
        {"ts": "100.1", "bot_id": "B1", "text": "Session s1 halted",
         "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "*Kill switch* on strategy 7"}}]},
        {"ts": "100.2", "user": "U2", "text": "<@U1> seen this?"},
        {"ts": "100.4", "user": "U1", "text": f"<@{BOT_USER}> why did it halt?"},
    )
    q = worker.build_question("C1", "100.1", "100.4", BOT_USER)
    assert q == ("Slack thread so far (oldest first):\n"
                 "[Gnome Bot] Session s1 halted\n*Kill switch* on strategy 7\n"
                 "[bob] @alice seen this?\n\n"
                 "Message to answer, from alice:\nwhy did it halt?")


def test_a_long_thread_keeps_its_root_and_latest_messages(fake_slack, monkeypatch):
    monkeypatch.setattr(worker, "MAX_HISTORY", 3)
    seed_thread(fake_slack, *[{"ts": f"100.{i}", "user": "U1", "text": f"m{i}"} for i in range(1, 8)])
    q = worker.build_question("C1", "100.1", "100.7", BOT_USER)
    assert "[U1] m1\n[…earlier messages left out…]\n[U1] m5\n[U1] m6\n\n" in q


def alert(**overrides) -> dict:
    return {"alertId": 41, "key": "FATAL_ERROR:s1", "type": "FATAL_ERROR", "severity": "CRITICAL",
            "title": "Session s1 died", "body": "NullPointerException at Foo", "mode": "live", "strategyId": 7,
            "sessionId": "s1", "listingId": None, "exchangeId": None, "accountId": None, "labels": {},
            "runbook": {"summary": "It died.", "firstSteps": ["Read its logs."],
                        "url": "https://github.com/gnome-trading-group/gnome-registry/blob/main/docs/runbooks/alerts/FATAL_ERROR.md"},
            "slackChannel": "CALERTS", "slackTs": "500.1", "firstPost": True, "stage": "dev", **overrides}


@pytest.fixture
def triaging(config_with, monkeypatch):
    config_with.feature_flags.triage_enabled = True
    runs = []
    monkeypatch.setattr(agent, "run", lambda cfg, role, q, tools, extra=None: runs.append((role, q, extra))
                        or agent.Answer("*Likely cause:* a bug.", 1200, "anthropic"))
    return runs


def test_triages_an_alert_in_its_own_thread(store, fake_slack, triaging):
    worker.triage_alert(alert(), store)
    assert fake_slack.reactions == [("add", "CALERTS", "500.1", "eyes"), ("remove", "CALERTS", "500.1", "eyes")]
    assert fake_slack.posts[0]["thread_ts"] == "500.1" and fake_slack.posts[0]["text"] == "*Likely cause:* a bug."
    role, prompt, extra = triaging[0]
    assert role == "triage" and '"sessionId": "s1"' in prompt and "not instructions" in prompt
    assert extra.startswith(worker.TRIAGE_INSTRUCTIONS) and memory.INSTRUCTIONS in extra
    assert store.is_tracked_thread("CALERTS", "500.1") and store.tokens_today() == 1200


def test_points_a_repeat_at_the_earlier_triage_during_the_cooldown(store, fake_slack, triaging, monkeypatch):
    monkeypatch.setattr(worker.slack, "permalink", lambda channel, ts: f"https://slack/{channel}/{ts}")
    worker.triage_alert(alert(), store)
    worker.triage_alert(alert(slackTs="600.1", sessionId="s2", key="FATAL_ERROR:s2"), store)
    assert len(triaging) == 1
    assert fake_slack.posts[-1]["thread_ts"] == "600.1" and "https://slack/CALERTS/500.1" in fake_slack.posts[-1]["text"]


def test_a_different_strategy_or_type_is_a_different_problem(store, fake_slack, triaging):
    worker.triage_alert(alert(), store)
    worker.triage_alert(alert(slackTs="600.1", strategyId=8), store)
    worker.triage_alert(alert(slackTs="700.1", type="SESSION_STALE", key="SESSION_STALE:s1"), store)
    assert len(triaging) == 3


@pytest.mark.parametrize("change", [
    {"severity": "INFO"},
    {"stage": "prod"},
])
def test_skips_alerts_below_the_bar_or_from_another_stage(store, fake_slack, triaging, change):
    worker.triage_alert(alert(**change), store)
    assert triaging == [] and fake_slack.posts == []


def test_triage_stays_off_until_its_flag_is_on(store, fake_slack, triaging, config_with):
    config_with.feature_flags.triage_enabled = False
    worker.triage_alert(alert(), store)
    assert triaging == [] and fake_slack.posts == []


def test_the_handler_routes_eventbridge_alerts_to_triage(aws, fake_slack, triaging, monkeypatch):
    monkeypatch.setattr(worker, "_store", None)
    worker.handler({"Records": [{"body": json.dumps({"detail-type": "AlertDelivered", "detail": alert()})}]}, None)
    assert triaging[0][0] == "triage"

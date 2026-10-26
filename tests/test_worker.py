import pytest

from bot import agent, alerts, llm, slack
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


def test_answers_in_the_thread_by_replacing_the_placeholder(store, fake_slack, config_with, monkeypatch):
    seed_thread(fake_slack, {"ts": "100.1", "user": "U1", "text": f"<@{BOT_USER}> what's pnl today?"})
    asked = []
    monkeypatch.setattr(agent, "run", lambda cfg, role, q, tools: asked.append((role, q)) or agent.Answer("Up $12.", 900, "anthropic"))

    worker.answer_question(question(), store)

    assert fake_slack.posts[0]["text"] == worker.PLACEHOLDER and fake_slack.posts[0]["thread_ts"] == "100.1"
    assert fake_slack.updates == [{"channel": "C1", "ts": fake_slack.posts[0]["ts"], "text": "Up $12."}]
    assert asked[0][0] == "qa" and asked[0][1].endswith("from U1:\nwhat's pnl today?")
    assert store.tokens_today() == 900
    assert store.is_tracked_thread("C1", "100.1")


def test_says_so_when_questions_are_switched_off(store, fake_slack, config_with):
    config_with.feature_flags.qa_enabled = False
    worker.answer_question(question(), store)
    assert "qa_enabled" in fake_slack.posts[0]["text"] and fake_slack.updates == []
    assert not store.is_tracked_thread("C1", "100.1")


def test_stops_at_the_daily_token_budget(store, fake_slack, config_with, sent_alerts, monkeypatch):
    config_with.limits.daily_token_budget = 1000
    store.add_tokens(1000)
    monkeypatch.setattr(agent, "run", lambda *a: pytest.fail("ran over budget"))
    worker.answer_question(question(), store)
    assert "token budget" in fake_slack.updates[0]["text"] and sent_alerts == ["over-budget"]


def test_reports_an_unreachable_model_once_and_answers_the_thread(store, fake_slack, config_with, sent_alerts,
                                                                   monkeypatch):
    seed_thread(fake_slack, {"ts": "100.1", "user": "U1", "text": "pnl?"})

    def unavailable(*_):
        raise llm.ModelUnavailable("Your credit balance is too low")
    monkeypatch.setattr(agent, "run", unavailable)

    worker.answer_question(question(), store)
    assert "can't reach my model" in fake_slack.updates[0]["text"] and sent_alerts == ["model-unavailable"]


def test_answers_an_unexpected_failure_instead_of_raising_into_a_retry(store, fake_slack, config_with, monkeypatch):
    seed_thread(fake_slack, {"ts": "100.1", "user": "U1", "text": "pnl?"})
    monkeypatch.setattr(agent, "run", lambda *a: 1 / 0)
    worker.answer_question(question(), store)
    assert "went wrong" in fake_slack.updates[0]["text"]


def test_asks_to_be_invited_when_it_cant_read_the_channel(store, fake_slack, config_with, monkeypatch):
    def not_a_member(*args, **kwargs):
        raise slack.SlackError("Slack API error (conversations.replies): not_in_channel")
    monkeypatch.setattr(slack, "thread_messages", not_a_member)
    worker.answer_question(question(), store)
    assert "/invite @Gnome Bot" in fake_slack.updates[0]["text"]


def test_the_question_carries_the_thread_with_names_and_without_placeholders(fake_slack):
    fake_slack.names = {"U1": "alice", "U2": "bob"}
    seed_thread(
        fake_slack,
        {"ts": "100.1", "bot_id": "B1", "text": "Session s1 halted",
         "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "*Kill switch* on strategy 7"}}]},
        {"ts": "100.2", "user": "U2", "text": "<@U1> seen this?"},
        {"ts": "100.3", "bot_id": "B1", "text": worker.PLACEHOLDER},
        {"ts": "100.4", "user": "U1", "text": f"<@{BOT_USER}> why did it halt?"},
    )
    q = worker.build_question("C1", "100.1", "100.4", BOT_USER, skip_ts=set())
    assert q == ("Slack thread so far (oldest first):\n"
                 "[Gnome Bot] Session s1 halted\n*Kill switch* on strategy 7\n"
                 "[bob] @alice seen this?\n\n"
                 "Message to answer, from alice:\nwhy did it halt?")


def test_a_long_thread_keeps_its_root_and_latest_messages(fake_slack, monkeypatch):
    monkeypatch.setattr(worker, "MAX_HISTORY", 3)
    seed_thread(fake_slack, *[{"ts": f"100.{i}", "user": "U1", "text": f"m{i}"} for i in range(1, 8)])
    q = worker.build_question("C1", "100.1", "100.7", BOT_USER, skip_ts=set())
    assert "[U1] m1\n[…earlier messages left out…]\n[U1] m5\n[U1] m6\n\n" in q

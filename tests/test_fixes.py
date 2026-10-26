import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from bot import agent, fixes, github, slack
from bot.handlers import github_callback, interact, worker
from tests.conftest import BOT_USER

CONTEXT = {"repos": ["gnome-registry"], "alert": {"type": "FATAL_ERROR", "sessionId": "s1"},
           "triage": "*Likely cause:* a bug in ledger-batch."}


@pytest.fixture
def config_with(monkeypatch, bot_config):
    bot_config.feature_flags.fix_enabled = True
    bot_config.feature_flags.triage_enabled = True
    monkeypatch.setattr(worker.runtime_config, "refresh", lambda: None)
    monkeypatch.setattr(worker.runtime_config, "_config", bot_config)
    return bot_config


@pytest.fixture
def dispatched(monkeypatch, fake_slack):
    runs = []
    monkeypatch.setattr(github, "dispatch_workflow",
                        lambda workflow, inputs: runs.append((workflow, inputs)) or "https://github.com/run/9")
    monkeypatch.setattr(slack, "user_email", lambda user_id: "op@gnometrading.group")
    return runs


@pytest.mark.parametrize("answer,expected", [
    ("*Likely cause:* x\n\nRepo: gnome-registry", ("*Likely cause:* x", ["gnome-registry"])),
    ("*Likely cause:* x\nRepo: `gnome-oms`", ("*Likely cause:* x", ["gnome-oms"])),
    ("*Likely cause:* x\nRepos: gnomepy, `gnome-classifier`", ("*Likely cause:* x", ["gnomepy", "gnome-classifier"])),
    ("*Likely cause:* x\nRepo: a, b, c, d, a", ("*Likely cause:* x", ["a", "b", "c"])),
    ("*Likely cause:* x\nRepo: the registry API", ("*Likely cause:* x", [])),
    ("*Likely cause:* venue down\nRepo: none", ("*Likely cause:* venue down", [])),
    ("*Likely cause:* x", ("*Likely cause:* x", [])),
])
def test_the_triage_names_its_repo_on_a_closing_line(answer, expected):
    assert fixes.split_repo_line(answer) == expected


@pytest.mark.parametrize("text,expected", [
    (f"<@{BOT_USER}> fix this", True), ("fix it please", True), ("Attempt fix", True), ("please fix this", True),
    ("can you explain the fix?", False), ("prefix this", False),
])
def test_recognizes_a_request_for_a_fix(text, expected):
    assert fixes.is_fix_request(text) is expected


def test_starts_the_fix_workflow_with_the_triage_and_says_so(store, config_with, dispatched, fake_slack):
    store.save_fix_context("C1", "500.1", CONTEXT)
    fixes.request_fix(config_with, store, "C1", "500.1", "U1")
    workflow, inputs = dispatched[0]
    assert workflow == "fix.yml"
    assert inputs["repos"] == "gnome-registry" and inputs["requested_by"] == "op@gnometrading.group"
    assert inputs["provider"] == "subscription" and inputs["model"] == "claude-sonnet-5-5"
    assert "ledger-batch" in inputs["task"] and '"sessionId": "s1"' in inputs["task"]
    assert "gnome-registry" in fake_slack.posts[-1]["text"] and "https://github.com/run/9" in fake_slack.posts[-1]["text"]


def test_one_fix_at_a_time_per_thread(store, config_with, dispatched, fake_slack):
    store.save_fix_context("C1", "500.1", CONTEXT)
    fixes.request_fix(config_with, store, "C1", "500.1", "U1")
    fixes.request_fix(config_with, store, "C1", "500.1", "U2")
    assert len(dispatched) == 1 and "already running" in fake_slack.posts[-1]["text"]


def test_stops_at_the_daily_fix_cap_without_holding_the_thread(store, config_with, dispatched, fake_slack):
    config_with.limits.max_fixes_per_day = 1
    for ts in ("500.1", "600.1"):
        store.save_fix_context("C1", ts, CONTEXT)
    fixes.request_fix(config_with, store, "C1", "500.1", "U1")
    fixes.request_fix(config_with, store, "C1", "600.1", "U1")
    assert len(dispatched) == 1 and "daily limit" in fake_slack.posts[-1]["text"]
    assert store.claim_fix_run("C1", "600.1")


@pytest.mark.parametrize("prepare,message", [
    (lambda cfg, store: setattr(cfg.feature_flags, "fix_enabled", False), "switched off"),
    (lambda cfg, store: None, "don't have a triage"),
])
def test_explains_why_it_wont_start_a_fix(store, config_with, dispatched, fake_slack, prepare, message):
    prepare(config_with, store)
    fixes.request_fix(config_with, store, "C1", "500.1", "U1")
    assert dispatched == [] and message in fake_slack.posts[-1]["text"]


def test_a_failed_dispatch_frees_the_thread(store, config_with, fake_slack, monkeypatch):
    store.save_fix_context("C1", "500.1", CONTEXT)
    monkeypatch.setattr(slack, "user_email", lambda user_id: None)

    def refuse(*_):
        raise github.GitHubError(422, "Unexpected inputs")
    monkeypatch.setattr(github, "dispatch_workflow", refuse)
    fixes.request_fix(config_with, store, "C1", "500.1", "U1")
    assert "couldn't start" in fake_slack.posts[-1]["text"] and store.claim_fix_run("C1", "500.1")


def test_a_triage_naming_a_repo_offers_a_fix_and_keeps_its_context(store, fake_slack, config_with, monkeypatch):
    monkeypatch.setattr(agent, "run", lambda *a: agent.Answer("*Likely cause:* a bug.\n\nRepo: gnome-registry", 10, "x"))
    alert = {"alertId": 1, "key": "k", "type": "FATAL_ERROR", "severity": "CRITICAL", "strategyId": 7,
             "slackChannel": "CALERTS", "slackTs": "500.1", "stage": "dev"}
    worker.triage_alert(alert, store)
    update = fake_slack.updates[0]
    assert update["text"] == "*Likely cause:* a bug."
    assert update["blocks"][0]["text"]["text"] == "*Likely cause:* a bug."
    button = update["blocks"][-1]["elements"][0]
    assert button["action_id"] == "bot_fix" and json.loads(button["value"]) == {"channel": "CALERTS", "thread_ts": "500.1"}
    assert button["text"]["text"] == "Attempt fix (gnome-registry)"
    assert store.fix_context("CALERTS", "500.1")["repos"] == ["gnome-registry"]


def test_a_fix_across_repos_names_them_all_on_the_button_and_in_the_run(store, fake_slack, config_with, dispatched,
                                                                         monkeypatch):
    monkeypatch.setattr(agent, "run", lambda *a: agent.Answer("*Likely cause:* drift.\nRepo: gnomepy, gnome-classifier",
                                                              10, "x"))
    worker.triage_alert({"alertId": 1, "key": "k", "type": "FATAL_ERROR", "severity": "CRITICAL", "strategyId": 7,
                         "slackChannel": "CALERTS", "slackTs": "500.1", "stage": "dev"}, store)
    assert fake_slack.updates[0]["blocks"][-1]["elements"][0]["text"]["text"] == "Attempt fix (gnomepy, gnome-classifier)"
    fixes.request_fix(config_with, store, "CALERTS", "500.1", "U1")
    assert dispatched[0][1]["repos"] == "gnomepy,gnome-classifier"
    assert "`gnomepy`, `gnome-classifier`" in fake_slack.posts[-1]["text"]


def test_a_long_list_of_repos_keeps_the_button_label_within_slacks_limit():
    button = fixes.fix_button("C1", "1.1", ["gnome-registry-with-a-long-name", "gnome-classifier-also-long", "gnomepy"])
    assert button["elements"][0]["text"]["text"] == "Attempt fix"


def test_no_button_while_fixes_are_off_but_the_context_is_kept(store, fake_slack, config_with, monkeypatch):
    config_with.feature_flags.fix_enabled = False
    monkeypatch.setattr(agent, "run", lambda *a: agent.Answer("*Likely cause:* a bug.\nRepo: gnome-oms", 10, "x"))
    worker.triage_alert({"alertId": 1, "key": "k", "type": "FATAL_ERROR", "severity": "CRITICAL", "strategyId": 7,
                         "slackChannel": "CALERTS", "slackTs": "500.1", "stage": "dev"}, store)
    assert "blocks" not in fake_slack.updates[0] and store.fix_context("CALERTS", "500.1")["repos"] == ["gnome-oms"]


def test_fix_this_in_a_triage_thread_starts_a_fix_instead_of_an_answer(store, config_with, dispatched, fake_slack,
                                                                        monkeypatch):
    store.save_fix_context("C1", "500.1", CONTEXT)
    monkeypatch.setattr(agent, "run", lambda *a: pytest.fail("answered instead of fixing"))
    worker.answer_question({"kind": "question", "channel": "C1", "thread_ts": "500.1", "ts": "500.9", "user": "U1",
                            "text": f"<@{BOT_USER}> fix this", "bot_user_id": BOT_USER}, store)
    assert len(dispatched) == 1


def test_the_button_click_forwarded_by_the_launcher_starts_the_fix(aws, config_with, dispatched, monkeypatch):
    monkeypatch.setattr(interact, "_store", None)
    interact._store = interact.Store()
    interact._store.save_fix_context("C1", "500.1", CONTEXT)
    interact.handler({"user": {"id": "U1"}, "actions": [
        {"action_id": "bot_fix", "value": json.dumps({"channel": "C1", "thread_ts": "500.1"})}]}, None)
    assert dispatched[0][1]["thread_ts"] == "500.1"


# The callback trusts only GitHub-signed tokens from the fix workflow on main, for the run the body names.
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def github_keys(monkeypatch):
    class Keys:
        def get_signing_key_from_jwt(self, token):
            return type("Key", (), {"key": KEY.public_key()})()
    monkeypatch.setattr(fixes, "_jwks", Keys())


def run_token(**claims) -> str:
    now = int(time.time())
    return jwt.encode({"iss": fixes.OIDC_ISSUER, "aud": fixes.OIDC_AUDIENCE, "iat": now, "exp": now + 300,
                       "job_workflow_ref": sorted(fixes.REPORTING_WORKFLOW_REFS)[0], "run_id": "77", **claims}, KEY, algorithm="RS256")


def callback(token: str, body: dict) -> dict:
    return github_callback.handler({"headers": {"Authorization": f"Bearer {token}"}, "body": json.dumps(body)}, None)


RESULT = {"channel": "C1", "thread_ts": "500.1", "run_id": "77", "run_url": "https://github.com/run/77",
          "status": "pr_opened", "summary": "Fixed.",
          "prs": [{"repo": "gnome-registry", "url": "https://github.com/pr/5", "title": "Fix the ledger"}]}


def test_posts_the_draft_pr_in_the_thread_and_frees_it(aws, github_keys, fake_slack, monkeypatch):
    monkeypatch.setattr(github_callback, "_store", None)
    assert callback(run_token(), RESULT)["statusCode"] == 200
    assert "https://github.com/pr/5" in fake_slack.posts[-1]["text"] and fake_slack.posts[-1]["thread_ts"] == "500.1"
    assert github_callback._store.claim_fix_run("C1", "500.1")


@pytest.mark.parametrize("claims", [
    {"job_workflow_ref": "gnome-trading-group/gnome-bot/.github/workflows/other.yml@refs/heads/main"},
    {"job_workflow_ref": "gnome-trading-group/gnome-bot/.github/workflows/fix.yml@refs/heads/attacker"},
    {"run_id": "78"},
    {"aud": "someone-else"},
    {"iss": "https://example.com"},
])
def test_refuses_a_token_that_isnt_this_fix_run(aws, github_keys, fake_slack, claims):
    assert callback(run_token(**claims), RESULT)["statusCode"] == 403
    assert fake_slack.posts == []


def test_refuses_a_callback_with_no_token(aws, fake_slack):
    assert github_callback.handler({"headers": {}, "body": json.dumps(RESULT)}, None)["statusCode"] == 401


def test_lists_every_pr_of_a_fix_across_repos_in_merge_order(aws, github_keys, fake_slack, monkeypatch):
    monkeypatch.setattr(github_callback, "_store", None)
    prs = [{"repo": "gnomepy", "url": "https://github.com/pr/1", "title": "Fix"},
           {"repo": "gnome-classifier", "url": "https://github.com/pr/2", "title": "Fix"}]
    assert callback(run_token(), {**RESULT, "prs": prs})["statusCode"] == 200
    text = fake_slack.posts[-1]["text"]
    assert text.index("gnomepy") < text.index("gnome-classifier") and "Merge in this order" in text


def test_reports_a_run_that_changed_nothing(aws, github_keys, fake_slack, monkeypatch):
    monkeypatch.setattr(github_callback, "_store", None)
    callback(run_token(), {**RESULT, "status": "no_change", "prs": [], "summary": "The cause is a venue outage."})
    assert "didn't find a change" in fake_slack.posts[-1]["text"] and "venue outage" in fake_slack.posts[-1]["text"]


def question(text: str, ts: str = "800.1") -> dict:
    return {"kind": "question", "channel": "C1", "thread_ts": "800.1", "ts": ts, "user": "U1", "text": text,
            "bot_user_id": BOT_USER}


def test_a_change_asked_for_in_slack_offers_a_fix_from_the_answer(store, config_with, dispatched, fake_slack,
                                                                  monkeypatch):
    fake_slack.threads[("C1", "800.1")] = [
        {"ts": "800.1", "user": "U1", "text": f"<@{BOT_USER}> launcher Slack times should be ET, can you fix it?"}]
    asked = []
    monkeypatch.setattr(agent, "run", lambda cfg, role, q, tools, extra=None: asked.append(extra) or agent.Answer(
        "`format_time` in `slack_client.py` uses UTC; I'd switch it to America/New_York.\nRepo: gnome-launcher", 10, "x"))
    worker.answer_question(question(f"<@{BOT_USER}> launcher Slack times should be ET, can you fix it?"), store)

    update = fake_slack.updates[0]
    assert update["text"].endswith("America/New_York.") and "Repo:" not in update["text"]
    assert update["blocks"][-1]["elements"][0]["text"]["text"] == "Attempt fix (gnome-launcher)"
    assert asked[0] == worker.CHANGE_REQUEST_INSTRUCTIONS

    fixes.request_fix(config_with, store, "C1", "800.1", "U1")
    task = dispatched[0][1]["task"]
    assert task.startswith("Requested in Slack:") and "times should be ET" in task and "format_time" in task


def test_an_ordinary_answer_offers_no_fix(store, config_with, fake_slack, monkeypatch):
    fake_slack.threads[("C1", "800.1")] = [{"ts": "800.1", "user": "U1", "text": "what's pnl?"}]
    monkeypatch.setattr(agent, "run", lambda *a: agent.Answer("Up $12 today.", 10, "x"))
    worker.answer_question(question("what's pnl?"), store)
    assert "blocks" not in fake_slack.updates[0] and store.fix_context("C1", "800.1") is None

import hashlib
import hmac
import json
from urllib.parse import urlencode

import boto3
import pytest

from bot import config, github, pr_assist
from bot.handlers import github_callback, github_webhook, worker
from tests.test_fixes import RESULT, callback, github_keys, run_token  # noqa: F401

WEBHOOK_SECRET = "webhook-secret"


@pytest.fixture
def secret(aws):
    boto3.client("secretsmanager").create_secret(Name="gnome-bot/github-webhook-secret", SecretString=WEBHOOK_SECRET)


@pytest.fixture
def queue(aws, monkeypatch):
    url = boto3.client("sqs").create_queue(QueueName="work")["QueueUrl"]
    monkeypatch.setattr(config, "WORK_QUEUE_URL", url)
    monkeypatch.setattr(github_webhook, "_sqs", None)
    return url


def queued(url) -> list[dict]:
    messages = boto3.client("sqs").receive_message(QueueUrl=url, MaxNumberOfMessages=10).get("Messages", [])
    return [json.loads(m["Body"]) for m in messages]


def comment_event(body="@gnome-bot handle the null case too", association="MEMBER", sender_type="User",
                  event="issue_comment", pull_request=True, owner="gnome-trading-group", action="created") -> tuple:
    payload = {
        "action": action,
        "comment": {"id": 501, "body": body, "author_association": association,
                    "html_url": "https://github.com/gnome-trading-group/gnome-registry/pull/7#issuecomment-1"},
        "sender": {"login": "mprey", "type": sender_type},
        "repository": {"name": "gnome-registry", "owner": {"login": owner}},
    }
    if event == "issue_comment":
        payload["issue"] = {"number": 7, **({"pull_request": {"url": "x"}} if pull_request else {})}
    else:
        payload["pull_request"] = {"number": 7}
    return event, payload


def delivery(event: str, payload: dict, secret: str = WEBHOOK_SECRET) -> dict:
    body = json.dumps(payload)
    signature = "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return {"headers": {"X-GitHub-Event": event, "X-Hub-Signature-256": signature}, "body": body}


def test_queues_an_org_members_mention_on_a_pr(secret, queue):
    assert github_webhook.handler(delivery(*comment_event()), None)["statusCode"] == 202
    assert queued(queue) == [{
        "kind": "pr_comment", "repo": "gnome-registry", "pr_number": "7",
        "comment": "@gnome-bot handle the null case too",
        "comment_url": "https://github.com/gnome-trading-group/gnome-registry/pull/7#issuecomment-1",
        "comment_id": "501", "review_comment": False, "requested_by": "mprey"}]


@pytest.mark.parametrize("body", [
    "@gnome-trading-bot handle the null case", "@gnome-trading-bot[bot] handle it", "thoughts, @Gnome-Bot?",
])
def test_answers_to_the_apps_name_and_the_short_name(secret, queue, body):
    github_webhook.handler(delivery(*comment_event(body=body)), None)
    assert queued(queue)[0]["comment"] == body


def test_queues_an_inline_review_comment_too(secret, queue):
    github_webhook.handler(delivery(*comment_event(event="pull_request_review_comment")), None)
    message = queued(queue)[0]
    assert message["pr_number"] == "7" and message["review_comment"] is True


def test_reads_a_form_encoded_delivery_githubs_default(secret, queue):
    event, payload = comment_event()
    body = urlencode({"payload": json.dumps(payload)})
    signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    response = github_webhook.handler({"headers": {"X-GitHub-Event": event, "X-Hub-Signature-256": signature,
                                                   "Content-Type": "application/x-www-form-urlencoded"},
                                       "body": body}, None)
    assert response["statusCode"] == 202 and queued(queue)[0]["pr_number"] == "7"


def test_a_signed_delivery_it_cant_read_is_a_400_not_a_crash(secret, queue):
    body = "not json"
    signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    assert github_webhook.handler({"headers": {"X-GitHub-Event": "ping", "X-Hub-Signature-256": signature},
                                   "body": body}, None)["statusCode"] == 400


def test_a_ping_is_accepted_and_ignored(secret, queue):
    assert github_webhook.handler(delivery("ping", {"zen": "Keep it logically awesome.", "hook_id": 1}), None)["statusCode"] == 202
    assert queued(queue) == []


def test_refuses_a_delivery_github_didnt_sign(secret, queue):
    assert github_webhook.handler(delivery(*comment_event(), secret="wrong"), None)["statusCode"] == 401
    assert queued(queue) == []


@pytest.mark.parametrize("change", [
    {"association": "CONTRIBUTOR"},
    {"association": "NONE"},
    {"sender_type": "Bot"},
    {"body": "looks good, thanks"},
    {"body": "email me at mason@gnome-bot.dev"},
    {"body": "see @gnome-trading-botanist"},
    {"pull_request": False},
    {"owner": "someone-else"},
    {"action": "edited"},
])
def test_ignores_comments_gnome_bot_doesnt_act_on(secret, queue, change):
    github_webhook.handler(delivery(*comment_event(**change)), None)
    assert queued(queue) == []


class FakeGitHub:
    """Records what gnome-bot says on the PR: reactions, new comments (ids from 900) and edits."""

    def __init__(self):
        self.reactions, self.comments = [], []

    def react(self, repo, comment_id, review_comment, content="eyes"):
        self.reactions.append((comment_id, review_comment, content))

    def comment(self, repo, pr_number, body):
        self.comments.append(body)
        return str(900 + len(self.comments))


@pytest.fixture
def pr(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(github, "react_to_comment", fake.react)
    monkeypatch.setattr(github, "comment_on_pr", fake.comment)
    return fake


@pytest.fixture
def dispatched(monkeypatch, pr):
    runs = []
    monkeypatch.setattr(github, "dispatch_workflow",
                        lambda workflow, inputs: runs.append((workflow, inputs)) or "https://github.com/run/9")
    return runs


def request() -> dict:
    return pr_assist.request_from(*comment_event())


def test_only_reacts_and_starts_the_run_which_posts_the_answer(store, bot_config, dispatched, pr):
    bot_config.feature_flags.fix_enabled = True
    pr_assist.handle(bot_config, store, request())
    assert pr.reactions == [("501", False, "eyes")] and pr.comments == []
    workflow, inputs = dispatched[0]
    assert workflow == "pr-assist.yml" and "reply_comment_id" not in inputs
    assert inputs["repo"] == "gnome-registry" and inputs["pr_number"] == "7" and inputs["requested_by"] == "mprey"
    assert "null case" in inputs["comment"] and inputs["model"] == "claude-sonnet-5-5"


def test_reacts_to_an_inline_review_comment_where_it_is(store, bot_config, dispatched, pr):
    bot_config.feature_flags.fix_enabled = True
    pr_assist.handle(bot_config, store, pr_assist.request_from(*comment_event(event="pull_request_review_comment")))
    assert pr.reactions == [("501", True, "eyes")]


@pytest.mark.parametrize("setup,why", [
    (lambda cfg, store: None, "switched off"),
    (lambda cfg, store: (setattr(cfg.feature_flags, "fix_enabled", True),
                         store.claim_fix_run(*pr_assist.run_key("gnome-registry", "7"))), "already working on this PR"),
    (lambda cfg, store: (setattr(cfg.feature_flags, "fix_enabled", True),
                         setattr(cfg.limits, "max_fixes_per_day", 0)), "daily limit"),
])
def test_says_on_the_pr_why_it_wont_run(store, bot_config, dispatched, pr, setup, why):
    setup(bot_config, store)
    pr_assist.handle(bot_config, store, request())
    assert dispatched == [] and pr.reactions == []
    assert len(pr.comments) == 1 and why in pr.comments[0] and pr.comments[0].startswith("@mprey, re [your comment]")


def test_a_run_that_wont_start_says_so_in_a_reply(store, bot_config, pr, monkeypatch):
    bot_config.feature_flags.fix_enabled = True

    def refuse(*_):
        raise github.GitHubError(422, "Unexpected inputs")
    monkeypatch.setattr(github, "dispatch_workflow", refuse)
    pr_assist.handle(bot_config, store, request())
    assert len(pr.comments) == 1 and "couldn't start the run" in pr.comments[0]
    assert store.claim_fix_run(*pr_assist.run_key("gnome-registry", "7"))


def test_the_run_still_starts_if_the_reaction_fails(store, bot_config, dispatched, monkeypatch):
    bot_config.feature_flags.fix_enabled = True

    def down(*args, **kwargs):
        raise github.GitHubError(502, "Bad gateway")
    monkeypatch.setattr(github, "react_to_comment", down)
    pr_assist.handle(bot_config, store, request())
    assert dispatched[0][0] == "pr-assist.yml"


def test_counts_against_the_daily_cap(store, bot_config, dispatched):
    bot_config.feature_flags.fix_enabled = True
    bot_config.limits.max_fixes_per_day = 1
    pr_assist.handle(bot_config, store, request())
    pr_assist.handle(bot_config, store, {**request(), "pr_number": "8"})
    assert len(dispatched) == 1


def test_the_worker_routes_pr_comments(aws, bot_config, dispatched, monkeypatch):
    bot_config.feature_flags.fix_enabled = True
    monkeypatch.setattr(worker.runtime_config, "refresh", lambda: None)
    monkeypatch.setattr(worker.runtime_config, "_config", bot_config)
    monkeypatch.setattr(worker, "_store", None)
    worker.handler({"Records": [{"body": json.dumps(request())}]}, None)
    assert dispatched[0][0] == "pr-assist.yml"


PR_RUN_TOKEN_REF = "gnome-trading-group/gnome-bot/.github/workflows/pr-assist.yml@refs/heads/main"


@pytest.mark.parametrize("replied", [True, False])
def test_the_runs_callback_frees_the_pr_and_replies_if_the_run_died_first(aws, github_keys, fake_slack, pr,  # noqa: F811
                                                                          monkeypatch, replied):
    monkeypatch.setattr(github_callback, "_store", None)
    github_callback._store = github_callback.Store()
    assert github_callback._store.claim_fix_run(*pr_assist.run_key("gnome-registry", "7"))
    response = callback(run_token(job_workflow_ref=PR_RUN_TOKEN_REF), {
        "repo": "gnome-registry", "pr_number": "7", "run_id": "77", "run_url": "https://github.com/run/77",
        "status": "success" if replied else "failure", "requested_by": "mprey",
        "comment_url": "https://github.com/c/1", "replied": replied})
    assert response["statusCode"] == 200 and fake_slack.posts == []
    assert github_callback._store.claim_fix_run(*pr_assist.run_key("gnome-registry", "7"))
    if replied:
        assert pr.comments == []
    else:
        assert pr.comments == ["@mprey, re [your comment](https://github.com/c/1): :x: The run failed before it could "
                               "answer ([run](https://github.com/run/77))."]

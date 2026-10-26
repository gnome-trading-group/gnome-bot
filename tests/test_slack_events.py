import hashlib
import hmac
import json
import time

import boto3
import pytest

from bot import config
from bot.handlers import slack_events
from tests.conftest import BOT_USER, SIGNING_SECRET


@pytest.fixture
def queue(aws, monkeypatch):
    url = boto3.client("sqs").create_queue(QueueName="work")["QueueUrl"]
    monkeypatch.setattr(config, "WORK_QUEUE_URL", url)
    monkeypatch.setattr(slack_events, "_store", None)
    monkeypatch.setattr(slack_events, "_sqs", None)
    return url


def queued(url) -> list[dict]:
    messages = boto3.client("sqs").receive_message(QueueUrl=url, MaxNumberOfMessages=10).get("Messages", [])
    return [json.loads(m["Body"]) for m in messages]


def request(payload: dict, secret: str = SIGNING_SECRET, timestamp: int | None = None) -> dict:
    body = json.dumps(payload)
    ts = str(timestamp if timestamp is not None else int(time.time()))
    signature = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return {"headers": {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": signature}, "body": body}


def callback(event: dict, event_id: str = "Ev1") -> dict:
    return {"type": "event_callback", "event_id": event_id, "authorizations": [{"user_id": BOT_USER}], "event": event}


def mention(**overrides) -> dict:
    return {"type": "app_mention", "user": "U1", "channel": "C1", "ts": "100.1", "text": f"<@{BOT_USER}> pnl?",
            **overrides}


def test_answers_slacks_url_verification(queue):
    response = slack_events.handler(request({"type": "url_verification", "challenge": "abc"}), None)
    assert json.loads(response["body"]) == {"challenge": "abc"}


@pytest.mark.parametrize("make", [
    lambda p: request(p, secret="wrong"),
    lambda p: request(p, timestamp=int(time.time()) - 600),
])
def test_refuses_a_bad_signature_or_a_stale_request(queue, make):
    assert slack_events.handler(make(callback(mention())), None)["statusCode"] == 401
    assert queued(queue) == []


def test_queues_a_mention_threaded_on_the_message(queue):
    assert slack_events.handler(request(callback(mention())), None)["statusCode"] == 200
    assert queued(queue) == [{"kind": "question", "channel": "C1", "thread_ts": "100.1", "ts": "100.1", "user": "U1",
                              "text": f"<@{BOT_USER}> pnl?", "bot_user_id": BOT_USER}]


def test_takes_each_slack_event_once(queue):
    slack_events.handler(request(callback(mention())), None)
    slack_events.handler(request(callback(mention())), None)
    assert len(queued(queue)) == 1


def test_queues_a_dm_without_a_mention(queue):
    dm = {"type": "message", "channel_type": "im", "user": "U1", "channel": "D1", "ts": "5.5", "text": "hi"}
    slack_events.handler(request(callback(dm)), None)
    assert queued(queue)[0]["channel"] == "D1"


def test_follows_up_without_a_mention_only_in_a_thread_the_bot_is_in(queue, store):
    reply = {"type": "message", "channel_type": "channel", "user": "U1", "channel": "C1", "ts": "101.0",
             "thread_ts": "100.1", "text": "and yesterday?"}
    slack_events.handler(request(callback(reply, "Ev1")), None)
    assert queued(queue) == []

    store.track_thread("C1", "100.1")
    slack_events.handler(request(callback(reply, "Ev2")), None)
    assert queued(queue)[0]["thread_ts"] == "100.1"


@pytest.mark.parametrize("event", [
    # The same message also arrives as app_mention, which is the one answered.
    {"type": "message", "channel_type": "channel", "user": "U1", "channel": "C1", "ts": "101.0", "thread_ts": "100.1",
     "text": f"<@{BOT_USER}> again"},
    {"type": "message", "channel_type": "channel", "user": "U1", "channel": "C1", "ts": "100.1", "text": "top level"},
    {"type": "message", "channel_type": "im", "bot_id": "B1", "channel": "D1", "ts": "5.5", "text": "from a bot"},
    {"type": "message", "channel_type": "im", "user": BOT_USER, "channel": "D1", "ts": "5.5", "text": "my own reply"},
    {"type": "message", "subtype": "message_changed", "channel_type": "im", "user": "U1", "channel": "D1", "ts": "5.5"},
    {"type": "reaction_added", "user": "U1", "channel": "C1", "ts": "5.5"},
])
def test_ignores_what_isnt_for_the_bot(queue, store, event):
    store.track_thread("C1", "100.1")
    slack_events.handler(request(callback(event)), None)
    assert queued(queue) == []

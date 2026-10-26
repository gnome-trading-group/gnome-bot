"""Flows run against moto's DynamoDB, SQS and Secrets Manager. Slack is an in-memory stand-in that records what it was
asked to post."""
import os

import boto3
import pytest
from moto import mock_aws

os.environ.update({
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "STAGE": "dev",
})

from bot import slack  # noqa: E402
from bot.aws_secrets import secret_string  # noqa: E402
from bot.runtime_config import BotConfig  # noqa: E402
from bot.store import Store  # noqa: E402

SIGNING_SECRET = "signing-secret"
BOT_USER = "UBOT"


@pytest.fixture
def aws():
    with mock_aws():
        secret_string.cache_clear()
        sm = boto3.client("secretsmanager")
        sm.create_secret(Name="slack-signing-secret", SecretString=SIGNING_SECRET)
        sm.create_secret(Name="slack-bot-token", SecretString="xoxb-test")
        boto3.client("dynamodb").create_table(
            TableName="gnome-bot",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield
        secret_string.cache_clear()


@pytest.fixture
def store(aws):
    return Store()


class FakeSlack:
    def __init__(self):
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self.threads: dict[tuple[str, str], list[dict]] = {}
        self.names: dict[str, str] = {}
        self._ts = 1000

    def post(self, channel, thread_ts, text, blocks=None):
        self._ts += 1
        ts = f"{self._ts}.000"
        self.posts.append({"channel": channel, "thread_ts": thread_ts, "text": text, "ts": ts})
        self.threads.setdefault((channel, thread_ts), []).append({"ts": ts, "bot_id": "B1", "text": text})
        return ts

    def update(self, channel, ts, text, blocks=None):
        self.updates.append({"channel": channel, "ts": ts, "text": text})

    def thread_messages(self, channel, thread_ts, limit):
        return self.threads.get((channel, thread_ts), [])[:limit]

    def user_name(self, user_id):
        return self.names.get(user_id, user_id)


@pytest.fixture
def fake_slack(monkeypatch):
    fake = FakeSlack()
    for name in ("post", "update", "thread_messages", "user_name"):
        monkeypatch.setattr(slack, name, getattr(fake, name))
    return fake


@pytest.fixture
def bot_config():
    cfg = BotConfig()
    cfg.feature_flags.qa_enabled = True
    return cfg

"""Slack's Events API endpoint. Slack wants a 200 within three seconds and retries otherwise, so this only checks the
signature, decides whether a message is for the bot, and queues it for the worker.

A message is for the bot when it @mentions it, is a DM to it, or replies in a thread the bot is already in (no
mention needed there). A mention in such a thread arrives as both app_mention and message; only the app_mention is
taken."""
import base64
import json
import logging

import boto3

from bot import config, slack
from bot.store import Store

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_sqs = None
_store: Store | None = None


def _ok(body: dict | None = None) -> dict:
    return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body or {})}


def question_for_bot(event: dict, bot_user_id: str, store: Store) -> dict | None:
    """The queued question for a Slack event, or None when the event isn't one the bot answers."""
    kind = event.get("type")
    if event.get("bot_id") or event.get("subtype") or not event.get("user") or event.get("user") == bot_user_id:
        return None
    channel, ts = event.get("channel"), event.get("ts")
    if not channel or not ts:
        return None
    if kind not in ("app_mention", "message"):
        return None
    if kind == "message" and event.get("channel_type") != "im":
        thread_ts = event.get("thread_ts")
        if not thread_ts or thread_ts == ts or f"<@{bot_user_id}>" in (event.get("text") or ""):
            return None
        if not store.is_tracked_thread(channel, thread_ts):
            return None
    return {"kind": "question", "channel": channel, "thread_ts": event.get("thread_ts") or ts, "ts": ts,
            "user": event["user"], "text": event.get("text") or ""}


def handler(event, context):
    global _sqs, _store
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    if not slack.verify_signature(body, headers.get("x-slack-request-timestamp", ""),
                                  headers.get("x-slack-signature", "")):
        return {"statusCode": 401, "body": "invalid signature"}

    payload = json.loads(body)
    if payload.get("type") == "url_verification":
        return _ok({"challenge": payload["challenge"]})
    if payload.get("type") != "event_callback":
        return _ok()

    _store = _store or Store()
    if not _store.claim_event(payload["event_id"]):
        return _ok()
    bot_user_id = ((payload.get("authorizations") or [{}])[0]).get("user_id", "")
    question = question_for_bot(payload.get("event") or {}, bot_user_id, _store)
    if question:
        _sqs = _sqs or boto3.client("sqs", region_name=config.REGION)
        _sqs.send_message(QueueUrl=config.WORK_QUEUE_URL, MessageBody=json.dumps({**question, "bot_user_id": bot_user_id}))
        logger.info("Queued a question in %s/%s", question["channel"], question["thread_ts"])
    return _ok()

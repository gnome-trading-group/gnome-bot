import hashlib
import hmac
import json
import logging
import re
import time

import requests

from bot import config
from bot.aws_secrets import secret_string

logger = logging.getLogger(__name__)

# Slack rejects requests whose timestamp is more than five minutes off, which also bounds replays.
MAX_REQUEST_AGE_SECONDS = 300
# chat.update refuses text over 40,000 characters; long answers are cut with a pointer to ask a narrower question.
MAX_TEXT = 39_000
# A section block holds at most 3,000 characters of text.
MAX_SECTION = 3000
MENTION = re.compile(r"<@([A-Z0-9]+)>")


class SlackError(RuntimeError):
    pass


def verify_signature(body: str, timestamp: str, signature: str, now: float | None = None) -> bool:
    try:
        age = abs((now if now is not None else time.time()) - int(timestamp))
    except ValueError:
        return False
    if age > MAX_REQUEST_AGE_SECONDS:
        return False
    secret = secret_string(config.SLACK_SIGNING_SECRET)
    expected = "v0=" + hmac.new(secret.encode(), f"v0:{timestamp}:{body}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _call(method: str, payload: dict, http_method: str = "POST") -> dict:
    headers = {"Authorization": f"Bearer {secret_string(config.SLACK_BOT_TOKEN_SECRET)}"}
    if http_method == "GET":
        response = requests.get(f"https://slack.com/api/{method}", headers=headers, params=payload, timeout=10)
    else:
        headers["Content-Type"] = "application/json; charset=utf-8"
        response = requests.post(f"https://slack.com/api/{method}", headers=headers, data=json.dumps(payload), timeout=10)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise SlackError(f"Slack API error ({method}): {data.get('error')}")
    return data


_BOLD = re.compile(r"\*\*(.+?)\*\*")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*$", re.MULTILINE)
_BULLET = re.compile(r"^(\s*)[-*]\s+", re.MULTILINE)
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def mrkdwn(text: str) -> str:
    """Slack's formatting from the Markdown models slip into: bold, headings, bullets and links."""
    text = _HEADING.sub(r"**\1**", text)
    text = _BULLET.sub(r"\1• ", text)
    text = _BOLD.sub(r"*\1*", text)
    return _LINK.sub(r"<\2|\1>", text)


def _clip(text: str) -> str:
    if len(text) <= MAX_TEXT:
        return text
    return text[:MAX_TEXT] + "\n\n_…cut off; ask something narrower for the rest._"


def post(channel: str, thread_ts: str | None, text: str, blocks: list | None = None) -> str:
    payload: dict = {"channel": channel, "text": _clip(text), "unfurl_links": False, "unfurl_media": False}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if blocks is not None:
        payload["blocks"] = blocks
    return _call("chat.postMessage", payload)["ts"]


def update(channel: str, ts: str, text: str, blocks: list | None = None) -> None:
    payload: dict = {"channel": channel, "ts": ts, "text": _clip(text)}
    # An empty list clears blocks a placeholder had; Slack then renders `text`.
    payload["blocks"] = blocks if blocks is not None else []
    _call("chat.update", payload)


def thread_messages(channel: str, thread_ts: str, limit: int) -> list[dict]:
    """The thread's messages, oldest first: the root and its earliest replies, `limit` in all."""
    return _call("conversations.replies", {"channel": channel, "ts": thread_ts, "limit": limit}, "GET")["messages"]


def permalink(channel: str, ts: str) -> str:
    return _call("chat.getPermalink", {"channel": channel, "message_ts": ts}, "GET")["permalink"]


def text_blocks(text: str) -> list[dict]:
    """Sections for `text`, split between paragraphs where it can be, so a long reply survives the section limit."""
    blocks, current = [], ""
    for paragraph in _clip(text).split("\n\n"):
        while len(paragraph) > MAX_SECTION:
            blocks.append(paragraph[:MAX_SECTION])
            paragraph = paragraph[MAX_SECTION:]
        if current and len(current) + 2 + len(paragraph) > MAX_SECTION:
            blocks.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        blocks.append(current)
    return [{"type": "section", "text": {"type": "mrkdwn", "text": chunk}} for chunk in blocks]


def user_email(user_id: str) -> str | None:
    try:
        return (_call("users.info", {"user": user_id}, "GET")["user"].get("profile") or {}).get("email")
    except Exception:
        logger.warning("Couldn't look up Slack user %s", user_id, exc_info=True)
        return None


def user_name(user_id: str) -> str:
    try:
        user = _call("users.info", {"user": user_id}, "GET")["user"]
    except Exception:
        logger.warning("Couldn't look up Slack user %s", user_id, exc_info=True)
        return user_id
    profile = user.get("profile") or {}
    return profile.get("display_name") or profile.get("real_name") or user.get("name") or user_id

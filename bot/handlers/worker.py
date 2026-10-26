"""Answers queued Slack questions. A placeholder goes into the thread at once and is replaced by the answer.

A failure after the placeholder is posted is answered in the thread and not retried: SQS redelivery would post a
second placeholder and re-spend the tokens. Out of credit is the same; it won't fix itself in a retry."""
import json
import logging
import re

from bot import agent, alerts, llm, slack
from bot.runtime_config import runtime_config
from bot.store import Store
from bot.tools import READ_TOOLS

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

PLACEHOLDER = ":hourglass_flowing_sand: checking…"
MAX_HISTORY = 30
# conversations.replies is oldest first; threads past this many replies lose their newest ones.
THREAD_FETCH_LIMIT = 200
MAX_MESSAGE_CHARS = 2000
MENTION = re.compile(r"<@([A-Z0-9]+)>")

_store: Store | None = None


def _message_text(message: dict) -> str:
    """A Slack message's words: its text, plus what its blocks say (alerts are mostly blocks)."""
    parts = [message.get("text") or ""]
    for block in message.get("blocks") or []:
        if block.get("type") == "rich_text":
            continue
        for item in [block.get("text"), *(block.get("fields") or []), *(block.get("elements") or [])]:
            if isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"] not in parts[0]:
                parts.append(item["text"])
    return "\n".join(p for p in parts if p).strip()


def build_question(channel: str, thread_ts: str, current_ts: str, bot_user_id: str, skip_ts: set[str]) -> str:
    """The thread as a transcript, then the message to answer. History goes in as text rather than as alternating
    turns: the thread can start with a bot message (an alert) and have several people in it."""
    names: dict[str, str] = {bot_user_id: "Gnome Bot"}

    def name(user_id: str) -> str:
        if user_id not in names:
            names[user_id] = slack.user_name(user_id)
        return names[user_id]

    def readable(message: dict) -> str:
        text = MENTION.sub(lambda m: "" if m.group(1) == bot_user_id else f"@{name(m.group(1))}", _message_text(message))
        return text.strip()[:MAX_MESSAGE_CHARS]

    history, current = [], None
    for message in slack.thread_messages(channel, thread_ts, limit=THREAD_FETCH_LIMIT):
        if message.get("ts") in skip_ts or (message.get("text") or "").startswith(PLACEHOLDER):
            continue
        author = "Gnome Bot" if message.get("bot_id") or message.get("user") == bot_user_id else name(message.get("user", ""))
        if message.get("ts") == current_ts:
            current = (author, readable(message))
        else:
            history.append(f"[{author}] {readable(message)}")
    if current is None:
        raise RuntimeError(f"Message {current_ts} isn't in thread {thread_ts}")
    # The root (often the alert being discussed) always stays, with the latest messages after it.
    kept = history if len(history) <= MAX_HISTORY else [history[0], "[…earlier messages left out…]",
                                                         *history[-(MAX_HISTORY - 1):]]
    transcript = "\n".join(kept)
    prefix = f"Slack thread so far (oldest first):\n{transcript}\n\n" if transcript else ""
    return f"{prefix}Message to answer, from {current[0]}:\n{current[1]}"


def answer_question(message: dict, store: Store) -> None:
    cfg = runtime_config.config
    channel, thread_ts = message["channel"], message["thread_ts"]
    if not cfg.feature_flags.qa_enabled:
        slack.post(channel, thread_ts, "I'm not answering questions yet (feature flag `qa_enabled` is off).")
        return

    placeholder = slack.post(channel, thread_ts, PLACEHOLDER)
    store.track_thread(channel, thread_ts)
    if store.tokens_today() >= cfg.limits.daily_token_budget:
        slack.update(channel, placeholder, "I've used up today's token budget, so I'm not answering until tomorrow (UTC).")
        alerts.once_a_day(store, "over-budget", "Gnome Bot is over its daily token budget",
                          f"Spent {store.tokens_today():,} of {cfg.limits.daily_token_budget:,} tokens today.")
        return

    try:
        question = build_question(channel, thread_ts, message["ts"], message["bot_user_id"], {placeholder})
        answer = agent.run(cfg, "qa", question, READ_TOOLS)
    except llm.ModelUnavailable as error:
        slack.update(channel, placeholder, "I can't reach my model right now (out of credit or down); try again later.")
        alerts.once_a_day(store, "model-unavailable", "Gnome Bot can't reach its model", str(error)[:1000])
        return
    except Exception:
        logger.exception("Couldn't answer %s/%s", channel, message["ts"])
        slack.update(channel, placeholder, "Something went wrong answering that; it's in my logs.")
        return

    store.add_tokens(answer.tokens)
    slack.update(channel, placeholder, slack.mrkdwn(answer.text))
    logger.info("Answered %s/%s with %s using %d tokens", channel, message["ts"], answer.provider, answer.tokens)


def handler(event, context):
    global _store
    _store = _store or Store()
    for record in event["Records"]:
        message = json.loads(record["body"])
        if message.get("kind") == "question":
            answer_question(message, _store)
        else:
            logger.warning("Ignoring unknown work item %s", message.get("kind"))

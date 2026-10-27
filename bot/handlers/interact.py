"""Gnome Bot's Slack buttons. The Slack app's one Interactivity URL belongs to the launcher, which checks the
signature and forwards every `bot_*` click here as an async invoke, so this never answers Slack itself."""
import json
import logging

from bot import fixes, memory
from bot.runtime_config import runtime_config
from bot.store import Store

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_store: Store | None = None


def handler(event, context):
    global _store
    _store = _store or Store()
    action = (event.get("actions") or [{}])[0]
    if action.get("action_id") in (memory.SAVE_ACTION_ID, memory.DISMISS_ACTION_ID):
        memory.handle_click(_store, event)
        return
    if action.get("action_id") != fixes.FIX_ACTION_ID:
        logger.warning("Ignoring unknown action %s", action.get("action_id"))
        return
    target = json.loads(action.get("value") or "{}")
    user_id = (event.get("user") or {}).get("id")
    if not target.get("channel") or not target.get("thread_ts") or not user_id:
        logger.warning("Ignoring a fix click without its thread or user")
        return
    fixes.request_fix(runtime_config.config, _store, target["channel"], target["thread_ts"], user_id)

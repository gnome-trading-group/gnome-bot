"""Where a fix run reports its result. The run signs nothing; it sends a GitHub OIDC token that names the run, which is
checked against GitHub's keys, the fix workflow on main and the run id in the body."""
import json
import logging

import jwt

from bot import fixes
from bot.store import Store

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

STATUSES = {"pr_opened", "no_change", "failed"}
_store: Store | None = None


def _response(status: int, message: str) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps({"message": message})}


def handler(event, context):
    global _store
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    token = headers.get("authorization", "").removeprefix("Bearer ").strip()
    try:
        result = json.loads(event.get("body") or "{}")
    except ValueError:
        return _response(400, "body is not JSON")
    if not token or not result.get("run_id"):
        return _response(401, "a run token and run_id are required")
    try:
        fixes.verify_run_token(token, result["run_id"])
    except jwt.PyJWTError as error:
        logger.warning("Refused a fix callback: %s", error)
        return _response(403, "not a gnome-bot fix run")
    if result.get("status") not in STATUSES or not result.get("channel") or not result.get("thread_ts"):
        return _response(400, f"status must be one of {sorted(STATUSES)}, with channel and thread_ts")
    if not isinstance(result.get("prs", []), list):
        return _response(400, "prs must be a list of {repo, url, title}")
    _store = _store or Store()
    fixes.report_result(result, _store)
    logger.info("Fix run %s for %s/%s: %s", result["run_id"], result["channel"], result["thread_ts"], result["status"])
    return _response(200, "reported")

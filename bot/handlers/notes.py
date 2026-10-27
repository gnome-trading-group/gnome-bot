"""The team notes API behind the controller's Tools → Bot Notes page: list, edit and delete. People only (the shared
API authorizer admits nothing else on these routes), and each change is attributed to the email on their token."""
import json
import logging
import re
from datetime import datetime, timezone

from bot import memory
from bot.store import Store

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,PATCH,DELETE,OPTIONS",
}
_store: Store | None = None


def _response(status: int, body) -> dict:
    return {"statusCode": status, "headers": {"Content-Type": "application/json", **CORS_HEADERS},
            "body": json.dumps(body)}


def _actor(event: dict) -> str | None:
    return ((event.get("requestContext") or {}).get("authorizer") or {}).get("email")


def _edit(store: Store, note_id: str, event: dict) -> dict:
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        return _response(400, {"message": "body is not JSON"})
    if not isinstance(body, dict):
        return _response(400, {"message": "body must be an object"})
    changes = {}
    if "note" in body:
        note = " ".join(str(body["note"] or "").split())
        if not 1 <= len(note) <= memory.MAX_NOTE_CHARS:
            return _response(400, {"message": f"note must be 1 to {memory.MAX_NOTE_CHARS} characters"})
        changes["note"] = note
    if "scope" in body:
        scope = str(body["scope"] or "").strip()
        if scope != memory.GENERAL and not memory.SCOPE.match(scope):
            return _response(400, {"message": "scope must be an alert type (letters, digits, _ . : -) or general"})
        changes["scope"] = scope
    if not changes:
        return _response(400, {"message": "nothing to change: send note and/or scope"})
    actor = _actor(event)
    changes |= {"edited_by": actor or "unknown", "edited_at": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    if not store.edit_memory(note_id, changes):
        return _response(404, {"message": f"no note {note_id}"})
    logger.info("Note %s edited by %s: %s", note_id, actor, sorted(changes))
    return _response(200, {"id": note_id, **store.memories()[note_id]})


def handler(event, context):
    global _store
    _store = _store or Store()
    method = event.get("httpMethod")
    note_id = (event.get("pathParameters") or {}).get("id")
    if note_id is not None and not re.fullmatch(memory.NOTE_ID, note_id):
        return _response(400, {"message": f"not a note id: {note_id!r}"})
    if method == "GET" and note_id is None:
        notes = sorted(_store.memories().items(), key=lambda item: item[1]["saved_at"], reverse=True)
        return _response(200, [{"id": i, **n} for i, n in notes])
    if method == "PATCH" and note_id:
        return _edit(_store, note_id, event)
    if method == "DELETE" and note_id:
        if not _store.delete_memory(note_id):
            return _response(404, {"message": f"no note {note_id}"})
        logger.info("Note %s deleted by %s", note_id, _actor(event))
        return _response(200, {"id": note_id, "deleted": True})
    return _response(405, {"message": f"{method} isn't supported here"})

import json

import pytest

from bot.handlers import notes


@pytest.fixture
def api(store, monkeypatch):
    monkeypatch.setattr(notes, "_store", store)
    store.save_memory("m-000001", {"scope": "COST_ANOMALY", "note": "Old.", "channel": "C1", "thread_ts": "1.1",
                                   "saved_by": "a@gnometrading.group", "saved_at": "2026-10-01"})
    store.save_memory("m-000002", {"scope": "general", "note": "New.", "channel": "C1", "thread_ts": "2.1",
                                   "saved_by": "a@gnometrading.group", "saved_at": "2026-10-27",
                                   "link": "https://slack/C1/2.1"})
    return store


def call(method, note_id=None, body=None):
    response = notes.handler({"httpMethod": method, "pathParameters": {"id": note_id} if note_id else None,
                              "body": json.dumps(body) if body is not None else None,
                              "requestContext": {"authorizer": {"kind": "PERSON", "email": "b@gnometrading.group"}}},
                             None)
    assert response["headers"]["Access-Control-Allow-Origin"] == "*"
    return response["statusCode"], json.loads(response["body"])


def test_lists_notes_newest_first(api):
    status, body = call("GET")
    assert status == 200 and [n["id"] for n in body] == ["m-000002", "m-000001"]
    assert body[0]["link"] == "https://slack/C1/2.1" and body[1]["link"] is None


def test_edits_a_notes_wording_and_scope_attributed_to_the_caller(api):
    status, body = call("PATCH", "m-000001", {"note": "  Shard   resizes. ", "scope": "COST_BUDGET"})
    assert status == 200 and body["note"] == "Shard resizes." and body["scope"] == "COST_BUDGET"
    assert body["edited_by"] == "b@gnometrading.group" and body["saved_by"] == "a@gnometrading.group"


@pytest.mark.parametrize("body", [{}, {"note": ""}, {"note": "x" * 501}, {"scope": "no spaces allowed"}, ["note"]])
def test_refuses_a_bad_edit(api, body):
    assert call("PATCH", "m-000001", body)[0] == 400
    assert api.memories()["m-000001"]["note"] == "Old."


def test_deletes_a_note_and_says_when_there_is_none(api):
    assert call("DELETE", "m-000001")[0] == 200
    assert "m-000001" not in api.memories()
    assert call("DELETE", "m-000001")[0] == 404
    assert call("PATCH", "m-000001", {"note": "x"})[0] == 404


def test_refuses_an_id_that_isnt_a_note_id(api):
    assert call("DELETE", "MEMORY#x")[0] == 400

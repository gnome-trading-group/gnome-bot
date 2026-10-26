import json
from types import SimpleNamespace

import pytest
from anthropic.lib.tools import ToolError

from bot import agent, gnome_api
from bot.tools import READ_TOOLS, common, docs, ops, trading


@pytest.fixture
def api(monkeypatch):
    calls = []

    def fake_get(api_name, path, params=None, headers=None):
        calls.append((api_name, path, {k: v for k, v in (params or {}).items() if v is not None}))
        return {"ok": True}
    monkeypatch.setattr(gnome_api, "get", fake_get)
    return calls


def test_every_tool_has_a_description_and_a_unique_name():
    names = [tool.name for tool in READ_TOOLS]
    assert len(names) == len(set(names))
    for tool in READ_TOOLS:
        assert tool.description and tool.to_dict()["input_schema"]["type"] == "object"


def test_firm_pnl_today_asks_for_the_mode_and_time_zone(api):
    trading.pnl_summary.call({"mode": "live"})
    assert api == [("registry", "pnl/summary", {"mode": "live", "tz": "America/New_York"})]


def test_a_session_pnl_asks_for_just_the_session(api):
    trading.pnl_summary.call({"mode": "live", "session": "0057e834-5436-4e6e-95ee-ad93c760a20c"})
    assert api == [("registry", "pnl/summary", {"sessionId": "0057e834-5436-4e6e-95ee-ad93c760a20c"})]


@pytest.mark.parametrize("tool,args", [
    # strategy-sessions pastes sessionId and status into SQL, so nothing but a plain id may reach it.
    (trading.sessions, {"session": "x' OR '1'='1"}),
    (trading.session_logs, {"session": "a;drop"}),
    (trading.risk_usage, {"session": ""}),
    (trading.pnl_summary, {"mode": "live", "tz": "Mars/Olympus"}),
    (trading.pnl_daily, {"mode": "live", "days": 0}),
    (trading.positions, {"strategy_id": 0, "mode": "live"}),
    (trading.alerts, {"strategy_ids": [-1]}),
])
def test_refuses_unsafe_or_out_of_range_inputs_before_calling_the_api(api, tool, args):
    with pytest.raises(ToolError):
        tool.call(args)
    assert api == []


def test_enum_inputs_are_checked_by_the_schema(api):
    # The tool runner hands this back to the model as an error result.
    with pytest.raises(ValueError):
        trading.sessions.call({"statuses": ["RUNNING'--"]})
    assert api == []


def test_session_statuses_are_sent_as_a_list_newest_first(api):
    trading.sessions.call({"statuses": ["RUNNING", "STARTING"], "strategy_id": 7})
    assert api[0][2] == {"status": "RUNNING,STARTING", "strategyId": 7, "sortBy": "started_at", "sortOrder": "desc",
                         "limit": 20}


def test_an_api_refusal_comes_back_to_the_model_as_a_tool_error(monkeypatch):
    def refuse(*args, **kwargs):
        raise gnome_api.ApiError(400, "pnl/summary", "mode is required")
    monkeypatch.setattr(gnome_api, "get", refuse)
    with pytest.raises(ToolError, match="mode is required"):
        trading.pnl_summary.call({"mode": "live"})


def test_long_results_are_cut_at_the_output_limit(monkeypatch):
    monkeypatch.setattr(common, "output_limit", 50)
    out = common.text({"rows": ["x" * 100]})
    assert out.startswith('{"rows":["xxxx') and "cut at 50" in out


@pytest.mark.parametrize("group", ["../etc", "/var/log/syslog", "secrets"])
def test_logs_are_limited_to_the_systems_own_groups(group):
    with pytest.raises(ToolError):
        ops.search_logs.call({"log_group": group})


@pytest.mark.parametrize("path", ["README.md", "src/x.md", "docs/../secrets.md"])
def test_read_doc_only_reads_claude_md_and_docs(path):
    with pytest.raises(ToolError):
        docs.read_doc.call({"repo": "gnome-registry", "path": path})


def message(stop_reason, *texts):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=t) for t in texts])


@pytest.mark.parametrize("msg,expected", [
    (message("end_turn", "Up $12."), "Up $12."),
    (message("tool_use", "Checking"), "Checking\n\n_I hit my limit of 12 steps before finishing. Try a narrower question._"),
    (message("refusal"), "I can't help with that one."),
    (message("max_tokens", "Long"), "Long\n\n_…cut off; ask for less detail._"),
])
def test_final_text_explains_why_an_answer_stopped(msg, expected):
    assert agent._final_text(msg, 12) == expected


def test_the_system_map_is_cached_after_the_prompt():
    blocks = agent.system_blocks()
    assert "dev environment" in blocks[0]["text"]
    assert blocks[1]["cache_control"] == {"type": "ephemeral"} and "gnome-registry" in blocks[1]["text"]
    assert json.dumps(blocks)

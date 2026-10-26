import anthropic
import httpx2
import pytest

from bot import llm
from bot.runtime_config import BotConfig, _from_dict, validate


def api_error(cls, message, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message, response=httpx2.Response(status, request=request), body=None)


@pytest.fixture
def clients(monkeypatch):
    monkeypatch.setattr(llm, "client_for", lambda provider: provider)


def test_runs_on_the_configured_provider_and_model(clients):
    result, provider = llm.run_with_fallback(BotConfig(), "qa", lambda client, model: (client, model))
    assert result == ("anthropic", "claude-haiku-5-5") and provider == "anthropic"


def test_falls_back_once_when_the_primary_is_out_of_credit(clients):
    cfg = BotConfig(fallback_provider="bedrock")
    calls = []

    def call(client, model):
        calls.append(client)
        if client == "anthropic":
            raise api_error(anthropic.BadRequestError, "Your credit balance is too low to access the Anthropic API.", 400)
        return model

    assert llm.run_with_fallback(cfg, "qa", call) == ("anthropic.claude-haiku-5-5", "bedrock")
    assert calls == ["anthropic", "bedrock"]


def test_unavailable_with_no_fallback(clients):
    def call(client, model):
        raise api_error(anthropic.InternalServerError, "overloaded", 500)
    with pytest.raises(llm.ModelUnavailable):
        llm.run_with_fallback(BotConfig(), "qa", call)


def test_our_own_bad_request_is_not_a_reason_to_switch_provider(clients):
    cfg = BotConfig(fallback_provider="bedrock")

    def call(client, model):
        raise api_error(anthropic.BadRequestError, "messages: roles must alternate", 400)
    with pytest.raises(anthropic.BadRequestError):
        llm.run_with_fallback(cfg, "qa", call)


def test_service_config_keeps_defaults_for_what_it_leaves_out():
    cfg = _from_dict(BotConfig, {"feature_flags": {"qa_enabled": True}, "limits": {"max_turns": 5}, "unknown": 1})
    assert cfg.feature_flags.qa_enabled and not cfg.feature_flags.triage_enabled
    assert cfg.limits.max_turns == 5 and cfg.limits.daily_token_budget == 30_000_000
    assert cfg.model_for("anthropic", "qa") == "claude-haiku-5-5"


@pytest.mark.parametrize("data", [
    {"provider": "openai"},
    {"fallback_provider": "nowhere"},
    {"models": {"anthropic": {"triage": "claude-haiku-5-5", "fix": "x"}}},
    {"fallback_provider": "subscription"},
])
def test_rejects_a_config_that_points_nowhere(data):
    with pytest.raises(ValueError):
        validate(_from_dict(BotConfig, data))

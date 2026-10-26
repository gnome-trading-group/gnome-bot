"""The bot's settings in the gnome service config system (service `bot`, edited on the controller's Service Config
page). Read with the bot's own token, refreshed at most every 30 seconds, and kept as last known when a refresh
fails. The defaults below are sent along, so a key the controller hasn't saved yet reads as its default."""
import base64
import dataclasses
import json
import logging
import time

from bot import gnome_api

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 30.0

PROVIDERS = ("anthropic", "bedrock", "subscription")
ROLES = ("qa", "triage", "fix")


@dataclasses.dataclass
class FeatureFlags:
    qa_enabled: bool = False
    triage_enabled: bool = False
    fix_enabled: bool = False


@dataclasses.dataclass
class Limits:
    daily_token_budget: int = 30_000_000
    max_fixes_per_day: int = 10
    max_turns: int = 12
    max_tool_output_chars: int = 20_000
    max_answer_tokens: int = 4096


@dataclasses.dataclass
class Triage:
    min_severity: str = "WARNING"
    cooldown_minutes: int = 30


def _default_models() -> dict[str, dict[str, str]]:
    return {
        "anthropic": {"qa": "claude-haiku-5-5", "triage": "claude-haiku-5-5", "fix": "claude-sonnet-5-5"},
        "subscription": {"fix": "claude-sonnet-5-5"},
        "bedrock": {
            "qa": "anthropic.claude-haiku-5-5",
            "triage": "anthropic.claude-haiku-5-5",
            "fix": "global.anthropic.claude-sonnet-5-5",
        },
    }


@dataclasses.dataclass
class BotConfig:
    feature_flags: FeatureFlags = dataclasses.field(default_factory=FeatureFlags)
    provider: str = "anthropic"
    fallback_provider: str | None = None
    fix_provider: str = "subscription"
    models: dict[str, dict[str, str]] = dataclasses.field(default_factory=_default_models)
    limits: Limits = dataclasses.field(default_factory=Limits)
    triage: Triage = dataclasses.field(default_factory=Triage)

    def model_for(self, provider: str, role: str) -> str:
        return self.models[provider][role]


def _from_dict(cls, data: dict):
    """A dataclass from a dict, ignoring unknown keys and keeping defaults for missing ones."""
    kwargs = {}
    for field in dataclasses.fields(cls):
        if field.name not in data:
            continue
        value = data[field.name]
        if dataclasses.is_dataclass(field.type) and isinstance(value, dict):
            kwargs[field.name] = _from_dict(field.type, value)
        else:
            kwargs[field.name] = value
    return cls(**kwargs)


def validate(cfg: BotConfig) -> None:
    """Raises on a config that would send requests nowhere, so a bad save keeps the last good config."""
    for name in ("provider", "fix_provider"):
        if getattr(cfg, name) not in PROVIDERS:
            raise ValueError(f"{name} must be one of {PROVIDERS}")
    if cfg.fallback_provider is not None and cfg.fallback_provider not in PROVIDERS:
        raise ValueError(f"fallback_provider must be null or one of {PROVIDERS}")
    for provider, role in [(cfg.provider, "qa"), (cfg.provider, "triage"), (cfg.fix_provider, "fix")]:
        if not isinstance(cfg.models.get(provider, {}).get(role), str):
            raise ValueError(f"models.{provider}.{role} must name a model")
    if cfg.fallback_provider is not None:
        for role in ("qa", "triage"):
            if not isinstance(cfg.models.get(cfg.fallback_provider, {}).get(role), str):
                raise ValueError(f"models.{cfg.fallback_provider}.{role} must name a model for the fallback")
    if not isinstance(cfg.feature_flags.qa_enabled, bool) or not isinstance(cfg.limits.daily_token_budget, int):
        raise ValueError("feature flags must be booleans and limits integers")


class RuntimeConfig:
    def __init__(self):
        self._config = BotConfig()
        self._last_refresh = 0.0
        self._defaults_header = base64.b64encode(json.dumps(dataclasses.asdict(BotConfig())).encode()).decode()

    @property
    def config(self) -> BotConfig:
        self.refresh()
        return self._config

    def refresh(self) -> None:
        now = time.monotonic()
        if self._last_refresh and now - self._last_refresh < REFRESH_INTERVAL_SECONDS:
            return
        self._last_refresh = now
        try:
            data = gnome_api.get("controller", "config/bot", headers={"x-config-defaults": self._defaults_header})
            cfg = _from_dict(BotConfig, data["config"])
            validate(cfg)
            self._config = cfg
        except Exception:
            logger.warning("Failed to refresh the bot's service config, keeping the last known one", exc_info=True)


runtime_config = RuntimeConfig()

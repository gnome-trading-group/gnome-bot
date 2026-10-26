"""Claude through either provider. Both clients expose the same `beta.messages.tool_runner`, so callers pick a
provider and a model id and never branch on which one they got."""
import functools
import logging
from typing import Callable, TypeVar

import anthropic
from anthropic.lib.bedrock import AnthropicBedrockMantle

from bot import config
from bot.aws_secrets import secret_string
from bot.runtime_config import BotConfig

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ModelUnavailable(RuntimeError):
    """No configured provider could answer: out of credit, refused credentials, or down after the SDK's retries."""


@functools.cache
def client_for(provider: str) -> anthropic.Anthropic | AnthropicBedrockMantle:
    if provider == "anthropic":
        return anthropic.Anthropic(api_key=secret_string(config.ANTHROPIC_API_KEY_SECRET))
    if provider == "bedrock":
        return AnthropicBedrockMantle(aws_region=config.REGION)
    raise ValueError(f"No API client for provider {provider!r}")


def is_unavailable(error: Exception) -> bool:
    """Errors a different provider might not have. A 400 is usually our request's fault, except running out of credit,
    which the API also reports as a 400."""
    if isinstance(error, (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                          anthropic.InternalServerError, anthropic.APIConnectionError, anthropic.RateLimitError)):
        return True
    return isinstance(error, anthropic.BadRequestError) and "credit balance" in str(error).lower()


def run_with_fallback(cfg: BotConfig, role: str, call: Callable[[object, str], T]) -> tuple[T, str]:
    """Runs `call(client, model)` on the configured provider, then once on the fallback provider if the first is
    unavailable. Returns the result and the provider that produced it."""
    providers = [cfg.provider] + ([cfg.fallback_provider] if cfg.fallback_provider else [])
    last_error: Exception | None = None
    for provider in providers:
        try:
            return call(client_for(provider), cfg.model_for(provider, role)), provider
        except Exception as error:
            if not is_unavailable(error):
                raise
            logger.warning("Provider %s unavailable for %s: %s", provider, role, error)
            last_error = error
    raise ModelUnavailable(str(last_error)) from last_error

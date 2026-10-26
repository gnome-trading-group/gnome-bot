"""Read-only calls to the registry, controller and launcher APIs as the bot: a Cognito client_credentials access token
with the gnome-api/bot.read scope, which the APIs' shared authorizer admits on the routes that allow bots."""
import logging
import time

import requests

from bot import config
from bot.aws_secrets import secret_json

logger = logging.getLogger(__name__)

SCOPE = "gnome-api/bot.read"
# A token is reused until this close to its expiry, so a request never carries one that lapses in flight.
EXPIRY_MARGIN_SECONDS = 120
REQUEST_TIMEOUT_SECONDS = 25

_token: str | None = None
_token_expires_at = 0.0


class ApiError(RuntimeError):
    """A refused or failed API call, carrying what the API said so the model can correct its request."""

    def __init__(self, status: int, path: str, message: str):
        super().__init__(f"{status} from {path}: {message}")
        self.status = status


def access_token() -> str:
    global _token, _token_expires_at
    if _token and time.time() < _token_expires_at - EXPIRY_MARGIN_SECONDS:
        return _token
    client = secret_json(config.COGNITO_CLIENT_SECRET)
    response = requests.post(
        client["token_url"],
        auth=(client["client_id"], client["client_secret"]),
        data={"grant_type": "client_credentials", "scope": SCOPE},
        timeout=10,
    )
    response.raise_for_status()
    body = response.json()
    _token = body["access_token"]
    _token_expires_at = time.time() + int(body["expires_in"])
    return _token


def _base_url(api: str) -> str:
    return {
        "registry": config.REGISTRY_API_URL,
        "controller": config.CONTROLLER_API_URL,
        "launcher": config.LAUNCHER_API_URL,
    }[api].rstrip("/")


def get(api: str, path: str, params: dict | None = None, headers: dict | None = None):
    """GET `path` on one of the three APIs; None-valued params are left out."""
    query = {k: v for k, v in (params or {}).items() if v is not None}
    response = requests.get(
        f"{_base_url(api)}/{path.lstrip('/')}",
        params=query,
        headers={"Authorization": access_token(), **(headers or {})},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        try:
            body = response.json()
            message = body.get("message") or body.get("Message") or body.get("error") or response.text
        except ValueError:
            message = response.text
        raise ApiError(response.status_code, path, str(message)[:500])
    return response.json()

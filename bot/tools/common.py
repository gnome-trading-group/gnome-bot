"""What every tool shares: validating inputs before they reach an API (some registry filters are pasted into SQL), and
turning results and refusals into the text the model reads."""
import json
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from anthropic.lib.tools import ToolError

from bot import gnome_api

SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_ID = 2_147_483_647

# Set per run from the service config's limits.max_tool_output_chars.
output_limit = 20_000


def text(data) -> str:
    """JSON for the model, cut at the output limit so one tool call can't flood the context."""
    rendered = data if isinstance(data, str) else json.dumps(data, default=str, separators=(",", ":"))
    if len(rendered) <= output_limit:
        return rendered
    return rendered[:output_limit] + f"\n…[cut at {output_limit} of {len(rendered)} characters; narrow the request]"


def call(api: str, path: str, params: dict | None = None):
    try:
        return gnome_api.get(api, path, params)
    except gnome_api.ApiError as error:
        raise ToolError(str(error)) from error


def session_id(value: str | None) -> str | None:
    if value is not None and not SESSION_ID.match(value):
        raise ToolError(f"Not a session id: {value!r}")
    return value


def positive_id(value: int | None, name: str) -> int | None:
    if value is not None and not 1 <= value <= MAX_ID:
        raise ToolError(f"{name} must be a positive integer id")
    return value


def id_list(values: list[int] | None, name: str) -> str | None:
    if not values:
        return None
    for value in values:
        positive_id(value, name)
    return ",".join(str(v) for v in values)


def time_zone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ToolError(f"Not an IANA time zone: {value!r}") from error
    return value


def bounded(value: int, low: int, high: int, name: str) -> int:
    if not low <= value <= high:
        raise ToolError(f"{name} must be between {low} and {high}")
    return value

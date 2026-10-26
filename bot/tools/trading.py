"""Read-only views of trading from the registry: PnL, sessions, positions, fills, orders, alerts, risk and treasury.
Numbers come back as scaled-integer strings: prices and money times 1e9, sizes and quantities times 1e6
(gnome-registry ledger-math.ts PRICE_SCALE and SIZE_SCALE)."""
from typing import Literal

from anthropic.lib.tools import beta_tool

from bot.tools.common import bounded, call, id_list, positive_id, session_id, text, time_zone

Mode = Literal["paper", "live"]
SessionStatus = Literal["SUBMITTED", "STARTING", "RUNNING", "STOPPING", "STOPPED", "FAILED"]
AlertStatus = Literal["PENDING", "FIRING", "RESOLVED"]
Severity = Literal["CRITICAL", "ERROR", "WARNING", "INFO"]

DEFAULT_TZ = "America/New_York"


@beta_tool
def pnl_summary(mode: Mode, strategy_id: int | None = None, session: str | None = None, tz: str = DEFAULT_TZ) -> str:
    """PnL now: the whole firm (just `mode`), one strategy (`strategy_id`), or one session (`session`, which ignores
    mode). Firm and strategy scopes include `today` since local midnight in `tz`, lifetime, realized, unrealized and
    fees; a session gives its totals since it started.

    Args:
        mode: paper (simulated) or live (real money) ledger.
        strategy_id: one strategy's PnL instead of the firm's.
        session: a strategy session id, for that session alone.
        tz: IANA time zone that defines "today".
    """
    params = {"sessionId": session_id(session)} if session else {
        "mode": mode, "strategyId": positive_id(strategy_id, "strategy_id"), "tz": time_zone(tz)}
    return text(call("registry", "pnl/summary", params))


@beta_tool
def pnl_daily(mode: Mode, days: int = 7, strategy_id: int | None = None, by_strategy: bool = False,
              tz: str = DEFAULT_TZ) -> str:
    """PnL per day, local midnight to midnight in `tz`; the last day runs to now.

    Args:
        mode: paper or live ledger.
        days: how many days back, 1 to 366.
        strategy_id: one strategy instead of the firm.
        by_strategy: also break each day down by strategy (firm scope only).
        tz: IANA time zone for day boundaries.
    """
    return text(call("registry", "pnl/daily", {
        "mode": mode, "days": bounded(days, 1, 366, "days"), "strategyId": positive_id(strategy_id, "strategy_id"),
        "byStrategy": "true" if by_strategy and strategy_id is None else None, "tz": time_zone(tz)}))


@beta_tool
def attention(mode: Mode) -> str:
    """What needs a person's attention now: open findings and alerts, unacknowledged and most severe first.

    Args:
        mode: paper or live.
    """
    return text(call("registry", "monitoring/attention", {"mode": mode}))


@beta_tool
def alerts(statuses: list[AlertStatus] | None = None, severities: list[Severity] | None = None,
           session: str | None = None, strategy_ids: list[int] | None = None, limit: int = 30) -> str:
    """Alerts, newest first, with title, body, severity, status, fire count, first/last seen and runbook URL.

    Args:
        statuses: only these statuses (default all).
        severities: only these severities (default all).
        session: only this session's alerts.
        strategy_ids: only these strategies' alerts.
        limit: 1 to 200.
    """
    return text(call("registry", "alerts", {
        "status": ",".join(statuses) if statuses else None, "severity": ",".join(severities) if severities else None,
        "sessionId": session_id(session), "strategyId": id_list(strategy_ids, "strategy_ids"),
        "limit": bounded(limit, 1, 200, "limit")}))


@beta_tool
def sessions(statuses: list[SessionStatus] | None = None, strategy_id: int | None = None, mode: Mode | None = None,
             session: str | None = None, limit: int = 20) -> str:
    """Strategy sessions (runs of a strategy on an instance), most recently started first: status, mode, instance,
    region, start/stop times and who stopped it and why.

    Args:
        statuses: only these statuses; RUNNING, STARTING and SUBMITTED are the live ones.
        strategy_id: only this strategy's sessions.
        mode: only paper or only live sessions.
        session: just this session.
        limit: 1 to 100.
    """
    return text(call("registry", "strategy-sessions", {
        "status": ",".join(statuses) if statuses else None, "strategyId": positive_id(strategy_id, "strategy_id"),
        "mode": mode, "sessionId": session_id(session), "sortBy": "started_at", "sortOrder": "desc",
        "limit": bounded(limit, 1, 100, "limit")}))


@beta_tool
def session_logs(session: str) -> str:
    """The latest log lines (up to 500) from a session's instance, with a CloudWatch console link.

    Args:
        session: the strategy session id.
    """
    return text(call("registry", "strategy-sessions/logs", {"sessionId": session_id(session)}))


@beta_tool
def risk_usage(session: str) -> str:
    """How close a session is to each risk limit, closest first, and whether risk has refused its orders.

    Args:
        session: the strategy session id.
    """
    return text(call("registry", "risk/usage", {"sessionId": session_id(session)}))


@beta_tool
def risk_policies(strategy_id: int | None = None, enabled: bool | None = None) -> str:
    """Risk policies (limits and kill switches) and their parameters.

    Args:
        strategy_id: only policies that apply to this strategy.
        enabled: only enabled (true) or disabled (false) policies.
    """
    return text(call("registry", "risk/policies", {
        "forStrategy": positive_id(strategy_id, "strategy_id"),
        "enabled": None if enabled is None else str(enabled).lower()}))


@beta_tool
def positions(strategy_id: int, mode: Mode, session: str | None = None) -> str:
    """A strategy's net positions per listing and account, flagged when one needs review.

    Args:
        strategy_id: the strategy.
        mode: paper or live.
        session: only the accounts this session trades.
    """
    return text(call("registry", "ledger/positions", {
        "strategyId": positive_id(strategy_id, "strategy_id"), "mode": mode, "sessionId": session_id(session)}))


@beta_tool
def fills(mode: Mode, strategy_id: int | None = None, session: str | None = None, listing_id: int | None = None,
          start: str | None = None, end: str | None = None, limit: int = 50) -> str:
    """Fills, newest first, with symbol, side (0 buy, 1 sell), price, quantity, fees, mark and slippage.

    Args:
        mode: paper or live (ignored when `session` is given).
        strategy_id: only this strategy.
        session: only this session.
        listing_id: only this listing.
        start: ISO-8601 UTC lower bound.
        end: ISO-8601 UTC upper bound.
        limit: 1 to 200.
    """
    params = {"sessionId": session_id(session)} if session else {
        "mode": mode, "strategyId": positive_id(strategy_id, "strategy_id")}
    params.update({"listingId": positive_id(listing_id, "listing_id"), "start": start, "end": end,
                   "limit": bounded(limit, 1, 200, "limit")})
    return text(call("registry", "ledger/fills", params))


@beta_tool
def orders(mode: Mode, status: Literal["OPEN", "CLOSED", "ANY"] = "ANY", strategy_id: int | None = None,
           session: str | None = None, listing_id: int | None = None, limit: int = 50) -> str:
    """Orders, newest first, with their fill quantity, average price and fees.

    Args:
        mode: paper or live (ignored when `session` is given).
        status: OPEN, CLOSED or ANY.
        strategy_id: only this strategy.
        session: only this session.
        listing_id: only this listing.
        limit: 1 to 200.
    """
    params = {"sessionId": session_id(session)} if session else {
        "mode": mode, "strategyId": positive_id(strategy_id, "strategy_id")}
    params.update({"status": status, "listingId": positive_id(listing_id, "listing_id"),
                   "limit": bounded(limit, 1, 200, "limit")})
    return text(call("registry", "ledger/orders/list", params))


@beta_tool
def treasury() -> str:
    """Every venue account's balances and its latest reconciliation against the venue (cash and position breaks)."""
    return text(call("registry", "treasury/summary"))


@beta_tool
def strategies(include_archived: bool = False) -> str:
    """Strategies by id and name, to turn a name someone uses into the id other tools take.

    Args:
        include_archived: also list archived strategies.
    """
    return text(call("registry", "strategies", None if include_archived else {"archived": "false"}))


@beta_tool
def listings(listing_ids: list[int]) -> str:
    """Listings by id, with their exchange and security symbol.

    Args:
        listing_ids: up to 100 listing ids.
    """
    if len(listing_ids) > 100:
        listing_ids = listing_ids[:100]
    return text(call("registry", "listings", {"listingIds": id_list(listing_ids, "listing_ids"), "denormalize": "true"}))


TOOLS = [pnl_summary, pnl_daily, attention, alerts, sessions, session_logs, risk_usage, risk_policies, positions,
         fills, orders, treasury, strategies, listings]

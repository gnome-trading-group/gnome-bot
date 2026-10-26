"""The bot's own problems, reported through the alerting system (gnomepy's send_alert, as a CUSTOM WARNING) at most
once a UTC day each. Alerting must never break an answer, so a report that can't be sent is only logged."""
import logging

from gnomepy.registry import RegistryClient

from bot.store import Store

logger = logging.getLogger(__name__)


def once_a_day(store: Store, key: str, title: str, body: str) -> None:
    try:
        if not store.claim_once_today(key):
            return
        RegistryClient().send_alert("CUSTOM", title, key=f"gnome-bot:{key}", severity="WARNING", body=body,
                                    actor="gnome-bot")
    except Exception:
        logger.warning("Couldn't report %s", key, exc_info=True)

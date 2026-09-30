"""Background polling loops. Each platform (and Demae-Can account) is polled
on its own interval, and each runs in its own task."""
import asyncio
import logging

from app import config
from app import db
from app.adapters.base import Adapter, NeedsReauth
from app.adapters.demaecan import DemaeCanAdapter, account_configs, account_session_key, translate_ja_en
from app.adapters.ubereats import UberEatsAdapter
from app import push

logger = logging.getLogger("pollers")

POLL_JOBS = []
if config.ENABLE_UBER_POLLING:
    POLL_JOBS.append(("ubereats", UberEatsAdapter, {}))
for acct in account_configs():
    POLL_JOBS.append(("demaecan", DemaeCanAdapter, {"account": acct}))

_event_subscribers: set[asyncio.Queue] = set()

_running = False
_state: dict[str, dict] = {}


def get_state() -> dict:
    return _state


def get_state_key(platform: str, adapter: Adapter) -> str:
    account = getattr(adapter, "account", "")
    return account_session_key(account) if account else platform


def subscribe() -> asyncio.Queue:
    q = asyncio.Queue(maxsize=200)
    _event_subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue):
    _event_subscribers.discard(q)


async def broadcast(event: dict):
    for q in list(_event_subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            q.get_nowait()
            q.put_nowait(event)


async def poll_platform(platform: str, adapter: Adapter):
    key = get_state_key(platform, adapter)
    backoff = 0
    while True:
        started = asyncio.get_event_loop().time()
        try:
            await poll_once(platform, key, adapter)
            backoff = 0
        except NeedsReauth as e:
            logger.warning("Reauth needed: %s", e)
            _state[key] = {**_state.get(key, {}), "auth": "needed", "error": str(e)}
            await broadcast({"type": "auth", "platform": platform, "account": key, "error": str(e)})
        except Exception as e:
            logger.exception("Poll failed for %s", key)
            _state[key] = {**_state.get(key, {}), "auth": "error", "error": str(e)}
            await broadcast({"type": "error", "platform": platform, "account": key, "error": str(e)})
            # exponential backoff on failure (e.g. rate limiting) so we don't
            # hammer the platform and can auto-recover once the limit resets
            backoff = min((backoff or 20) * 2, 300)
        elapsed = asyncio.get_event_loop().time() - started
        await asyncio.sleep(max(5, config.POLL_INTERVAL_SECONDS - elapsed + backoff))


_attempted_items: set[tuple[str, str]] = set()
_attempt_counts: dict[tuple[str, str], int] = {}
MAX_ITEM_ATTEMPTS = 8


async def poll_once(platform: str, key: str, adapter: Adapter):
    orders = await adapter.fetch_orders()
    enrich = getattr(adapter, "fetch_order_items", None)
    new_ids = []
    for o in orders:
        oid = o["external_id"]
        # Fetch item/meta details; retried on failure (capped), once on success.
        attempts = _attempt_counts.get((platform, oid), 0)
        if (
            enrich
            and (platform, oid) not in _attempted_items
            and attempts < MAX_ITEM_ATTEMPTS
            and not db.order_has_items(platform, oid)
        ):
            _attempt_counts[(platform, oid)] = attempts + 1
            try:
                details = getattr(adapter, "fetch_order_details", None)
                if details:
                    items, meta = await details(o)
                    if items:
                        o["items"] = items
                    if meta:
                        o["meta"] = meta
                else:
                    items = await enrich(o)
                    if items:
                        o["items"] = items
                _attempted_items.add((platform, oid))
            except Exception as e:
                logger.warning("item enrichment failed for %s %s: %s", platform, oid[:10], e)
        # Refresh the real status of in-flight Uber orders each poll.
        live_status = getattr(adapter, "fetch_live_status", None)
        if (
            live_status
            and platform == "ubereats"
            and o.get("status") in ("NEW", "ACCEPTED", "PREPARING", "READY", "PICKED_UP")
        ):
            try:
                st = await live_status(o)
                if st:
                    o["status"] = st
                    o["status_raw"] = st
            except Exception as e:
                logger.warning("live status refresh failed for %s %s: %s", platform, oid[:10], e)
        # Translated customer name for Uber orders (like Demae cards).
        if platform == "ubereats" and o.get("customer") and not o.get("meta"):
            try:
                en = await translate_ja_en(o["customer"])
                o["meta"] = {"customer": o["customer"], "customer_en": en}
            except Exception:
                pass
        is_new = db.upsert_order(platform, o)
        if is_new:
            new_ids.append(oid)
            await broadcast(
                {"type": "new_order", "platform": platform, "account": key, "order": o, "orders": orders}
            )
            try:
                push.send_new_order_push(o)
            except Exception as e:
                logger.warning("push failed: %s", e)
    _state[key] = {
        "auth": "ok",
        "orders_active": len(orders),
        "last_poll": _now(),
        "error": None,
    }
    await broadcast(
        {
            "type": "poll",
            "platform": platform,
            "account": key,
            "new": new_ids,
            "active": len(orders),
        }
    )


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


async def refresh_delivery_time(account: str, adapter: Adapter):
    """Refresh the delivery-time value and keep it in the DB (per account)."""
    key = account_session_key(account)
    while True:
        try:
            value = await adapter.fetch_delivery_time()
            if value is not None:
                db.set_delivery_time(key, value)
                await broadcast(
                    {"type": "delivery_time", "platform": "demaecan", "account": account, "value": value}
                )
        except NeedsReauth as e:
            logger.warning("Reauth needed: %s", e)
            _state[key] = {**_state.get(key, {}), "auth": "needed"}
        except Exception as e:
            logger.warning("Delivery-time refresh failed for %s: %s", account, e)
        await asyncio.sleep(15)


async def start():
    global _running
    if _running:
        return
    _running = True
    tasks = []
    for platform, cls, kwargs in POLL_JOBS:
        adapter = cls(**kwargs)
        tasks.append(asyncio.create_task(poll_platform(platform, adapter)))
        if platform == "demaecan":
            account = kwargs["account"]
            tasks.append(asyncio.create_task(refresh_delivery_time(account, adapter)))
    await asyncio.gather(*tasks)

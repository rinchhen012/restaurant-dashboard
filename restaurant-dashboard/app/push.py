"""Web Push (VAPID) helpers: key management, subscription storage, sending.

Lets the dashboard send real push notifications to the phone (even when the
browser is closed) when a new order arrives. The Mac server is the pusher,
so pushes only happen while the dashboard is running.
"""
import asyncio
import base64
import json
import logging

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import webpush

from app import config
from app import db

logger = logging.getLogger("push")

VAPID_FILE = config.DATA_DIR / "vapid.json"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def load_or_create_vapid() -> dict:
    if VAPID_FILE.exists():
        return json.loads(VAPID_FILE.read_text())
    key = ec.generate_private_key(ec.SECP256R1())
    private_der = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_point = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    vapid = {
        "private_key": _b64url(private_der),
        "public_key": _b64url(public_point),
    }
    config.save_json(VAPID_FILE, vapid)
    return vapid


def public_key() -> str:
    return load_or_create_vapid()["public_key"]


def save_subscription(subscription: dict):
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO push_subs (endpoint, keys_json, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(endpoint) DO UPDATE SET keys_json=excluded.keys_json, updated_at=datetime('now')
            """,
            (subscription["endpoint"], json.dumps(subscription.get("keys") or {}, ensure_ascii=False)),
        )
    logger.info("push subscription saved: %s", subscription.get("endpoint", "")[:60])


def remove_subscription(endpoint: str):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM push_subs WHERE endpoint=?", (endpoint,))


def _subscriptions() -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT endpoint, keys_json FROM push_subs").fetchall()
    out = []
    for r in rows:
        try:
            keys = json.loads(r["keys_json"])
        except Exception:
            continue
        if keys.get("p256dh") and keys.get("auth"):
            out.append({"endpoint": r["endpoint"], "keys": keys})
    return out


def _notify_order(order: dict) -> None:
    """Build the push payload for a new order."""
    platform = order.get("platform") or ""
    label = config.PLATFORMS.get(platform, {}).get("label", platform)
    store = order.get("store_name") or ""
    items = (order.get("items") or [])[:3]
    parts = []
    for i in items:
        qty = i.get("quantity", 1)
        parts.append(f"{qty}× {i.get('title', '')}")
    body = " · ".join(parts)
    if order.get("total") is not None:
        body = f"{body} · ¥{int(order['total']):,}" if body else f"¥{int(order['total']):,}"
    title = f"New {label} order" + (f" · {store}" if store else "")
    return {"title": title, "body": body or "New order"}


def _send_one(sub: dict, payload: dict) -> bool:
    vapid = load_or_create_vapid()
    try:
        webpush(
            subscription_info=sub,
            data=json.dumps(payload),
            vapid_private_key=vapid["private_key"],
            vapid_claims={"sub": "mailto:dashboard@restaurant.local"},
            timeout=10,
        )
        return True
    except Exception as e:
        # 404/410 = subscription gone (uninstalled/expired) -> drop it
        msg = str(e)
        if "404" in msg or "410" in msg:
            remove_subscription(sub["endpoint"])
        logger.warning("push failed (%s): %s", sub["endpoint"][:40], msg[:120])
        return False


def send_new_order_push(order: dict) -> None:
    """Send push notifications for a new order (best-effort, non-blocking)."""
    subs = _subscriptions()
    if not subs:
        return
    payload = _notify_order(order)
    for sub in subs:
        try:
            asyncio.to_thread(_send_one, sub, payload)
        except Exception:
            pass


def send_test() -> int:
    """Send a test push to every registered device; returns how many were sent."""
    subs = _subscriptions()
    payload = {"title": "Dashboard test notification", "body": "✅ Your phone is connected. New orders will notify you here."}
    sent = 0
    for sub in subs:
        if _send_one(sub, payload):
            sent += 1
    return sent
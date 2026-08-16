import json
import sqlite3
from contextlib import contextmanager

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    external_id TEXT NOT NULL,
    workflow_uuid TEXT,
    status TEXT NOT NULL DEFAULT 'UNKNOWN',
    status_raw TEXT,
    items_json TEXT,
    total REAL,
    currency TEXT,
    placed_at TEXT,
    updated_at TEXT DEFAULT (datetime('now')),
    first_seen_at TEXT DEFAULT (datetime('now')),
    UNIQUE(platform, external_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    platform TEXT PRIMARY KEY,
    cookies_json TEXT,
    captured_at TEXT,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS delivery_times (
    platform TEXT PRIMARY KEY,
    value_json TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # lightweight migrations: add columns if missing
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
        for col in ("store_uuid", "store_name", "meta_json", "workflow_uuid", "order_code"):
            if col not in cols:
                conn.execute(f"ALTER TABLE orders ADD COLUMN {col} TEXT")
        # self-healing: drop duplicate rows superseded by the same order
        # (external_id was the workflowUuid before the order got an orderUuid)
        conn.execute(
            """
            DELETE FROM orders
            WHERE external_id IN (
                SELECT o2.workflow_uuid FROM orders o2
                WHERE o2.workflow_uuid IS NOT NULL
                  AND o2.workflow_uuid != o2.external_id
            )
            """
        )


def upsert_order(platform: str, order: dict) -> bool:
    """Insert or update an order. Returns True if the order is NEW (first seen).

    Orders are matched by external_id; if missing, by workflow_uuid (Uber
    orders change external id from workflowUuid to orderUuid once accepted,
    which would otherwise create duplicate rows).
    """
    ext = order["external_id"]
    wf = order.get("workflow_uuid") or ""
    items_json = json.dumps(order.get("items", []), ensure_ascii=False)
    meta_json = json.dumps(order.get("meta") or {}, ensure_ascii=False)
    status = order.get("status", "UNKNOWN")
    status_raw = order.get("status_raw")
    total = order.get("total")
    currency = order.get("currency")
    placed_at = order.get("placed_at")
    store_uuid = order.get("store_uuid")
    store_name = order.get("store_name")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM orders WHERE platform=? AND external_id=?",
            (platform, ext),
        ).fetchone()
        if row is None and wf:
            row = conn.execute(
                "SELECT id FROM orders WHERE platform=? AND workflow_uuid=?",
                (platform, wf),
            ).fetchone()
        if row is None and wf:
            # legacy rows were created before workflow_uuid was stored, with
            # external_id == workflowUuid; treat them as the same order.
            row = conn.execute(
                "SELECT id FROM orders WHERE platform=? AND external_id=?",
                (platform, wf),
            ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO orders (platform, external_id, workflow_uuid, status, status_raw, items_json, meta_json, total, currency, placed_at, store_uuid, store_name, order_code)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (platform, ext, wf, status, status_raw, items_json, meta_json, total, currency, placed_at, store_uuid, store_name, order.get("order_code")),
            )
            return True
        conn.execute(
            """
            UPDATE orders SET
                external_id=?,
                workflow_uuid=COALESCE(?, workflow_uuid),
                status=?,
                status_raw=?,
                items_json=CASE WHEN ? = '[]' THEN items_json ELSE ? END,
                meta_json=CASE WHEN ? IN ('{}', '[]') THEN meta_json ELSE ? END,
                total=?,
                currency=?,
                placed_at=COALESCE(?, placed_at),
                store_uuid=COALESCE(?, store_uuid),
                store_name=COALESCE(?, store_name),
                order_code=COALESCE(?, order_code),
                updated_at=datetime('now')
            WHERE id=?
            """,
            (ext, wf, status, status_raw, items_json, items_json, meta_json, meta_json, total, currency, placed_at, store_uuid, store_name, order.get("order_code"), row["id"]),
        )
        return False


def order_has_items(platform: str, external_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT items_json FROM orders WHERE platform=? AND external_id=?",
            (platform, external_id),
        ).fetchone()
    if not row:
        return False
    try:
        items = json.loads(row["items_json"] or "[]")
    except Exception:
        return False
    return len(items) > 0


def list_orders(platform: str = None, limit: int = 100, today_only: bool = False, date: str = None) -> list[dict]:
    sql = "SELECT * FROM orders"
    params: list = []
    conds = []
    if platform:
        conds.append("platform=?")
        params.append(platform)
    if today_only:
        conds.append("date(COALESCE(placed_at, first_seen_at)) = date('now', 'localtime')")
    if date:
        conds.append("date(COALESCE(placed_at, first_seen_at)) = ?")
        params.append(date)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY COALESCE(placed_at, first_seen_at) DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for r in rows:
        try:
            r["items"] = json.loads(r.get("items_json") or "[]")
        except Exception:
            r["items"] = []
        try:
            r["meta"] = json.loads(r.get("meta_json") or "{}")
        except Exception:
            r["meta"] = {}
    return rows


def today_stats(platform: str = None, store_uuid: str = None) -> dict:
    """Order counts + revenue for today, per platform or total."""
    sql = """
        SELECT platform,
               COUNT(*) AS total_orders,
               COALESCE(SUM(total), 0) AS revenue
        FROM orders
        WHERE date(COALESCE(placed_at, first_seen_at)) = date('now', 'localtime')
    """
    params: list = []
    if platform:
        sql += " AND platform=?"
        params.append(platform)
    if store_uuid:
        sql += " AND store_uuid=?"
        params.append(store_uuid)
    sql += " GROUP BY platform"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    stats: dict[str, dict] = {}
    for r in rows:
        stats[r["platform"]] = {"orders": r["total_orders"], "revenue": round(r["revenue"] or 0, 2)}
    if platform:
        return stats.get(platform, {"orders": 0, "revenue": 0.0})
    return stats


def per_store_stats(platform: str, stores: list[dict]) -> list[dict]:
    """Today stats per store uuid, preserving the order of `stores`.
    Orders without a store are bucketed as an 'Unknown store' entry."""
    sql = """
        SELECT COALESCE(store_uuid, '__unknown__') AS store_uuid,
               COUNT(*) AS n, COALESCE(SUM(total), 0) AS revenue
        FROM orders
        WHERE platform=?
          AND date(COALESCE(placed_at, first_seen_at)) = date('now', 'localtime')
        GROUP BY store_uuid
    """
    with get_conn() as conn:
        rows = {r["store_uuid"]: r for r in conn.execute(sql, (platform,)).fetchall()}
    out = []
    for s in stores:
        r = rows.get(s["uuid"])
        out.append(
            {
                **s,
                "orders": r["n"] if r else 0,
                "revenue": round(r["revenue"] or 0, 2) if r else 0.0,
            }
        )
    unknown = rows.get("__unknown__")
    if unknown:
        out.append(
            {
                "uuid": "__unknown__",
                "label": "Unknown store",
                "name": "",
                "orders": unknown["n"],
                "revenue": round(unknown["revenue"] or 0, 2),
            }
        )
    return out


def status_counts(platform: str, store_uuid: str = None) -> dict[str, int]:
    sql = "SELECT status, COUNT(*) AS n FROM orders WHERE platform=?"
    params = [platform]
    if store_uuid:
        sql += " AND store_uuid=?"
        params.append(store_uuid)
    sql += " GROUP BY status"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {r["status"]: r["n"] for r in rows}


def set_delivery_time(platform: str, value: dict):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO delivery_times (platform, value_json, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(platform) DO UPDATE SET value_json=excluded.value_json, updated_at=datetime('now')
            """,
            (platform, json.dumps(value, ensure_ascii=False)),
        )


def get_delivery_time(platform: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value_json, updated_at FROM delivery_times WHERE platform=?", (platform,)
        ).fetchone()
    if not row:
        return None
    value = json.loads(row["value_json"])
    value["updated_at"] = row["updated_at"]
    return value


def get_setting(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )


def save_session(platform: str, cookies: list[dict], expires_at: str | None = None):
    from app import config as cfg

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sessions (platform, cookies_json, captured_at, expires_at)
            VALUES (?, ?, datetime('now'), ?)
            ON CONFLICT(platform) DO UPDATE SET
                cookies_json=excluded.cookies_json,
                captured_at=datetime('now'),
                expires_at=excluded.expires_at
            """,
            (platform, cfg.encrypt_text(json.dumps(cookies)), expires_at),
        )


def load_session(platform: str) -> list[dict] | None:
    from app import config as cfg

    with get_conn() as conn:
        row = conn.execute(
            "SELECT cookies_json FROM sessions WHERE platform=?", (platform,)
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(cfg.decrypt_text(row["cookies_json"]))
    except Exception:
        return None


def session_info(platform: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT captured_at, expires_at FROM sessions WHERE platform=?", (platform,)
        ).fetchone()
    return dict(row) if row else None


def delete_session(platform: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE platform=?", (platform,))

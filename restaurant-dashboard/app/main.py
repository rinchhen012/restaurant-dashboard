import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app import config
from app import db
from app import pollers
from app.adapters.demaecan import DemaeCanAdapter, account_configs, account_session_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    task = asyncio.create_task(pollers.start())
    yield
    task.cancel()


app = FastAPI(title="Restaurant Order Dashboard", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"


def load_endpoints() -> dict:
    return config.load_json(config.DATA_DIR / "endpoints.json", default={})


def platform_state(platform: str) -> dict:
    """Aggregate poller state; for demaecan merge all account states."""
    state = pollers.get_state()
    if platform == "demaecan":
        keys = [account_session_key(a) for a in account_configs()] or ["demaecan"]
        parts = [state.get(k, {}) for k in keys]
        if not any(p for p in parts):
            return {"auth": "unknown", "error": None}
        auth = "needed" if any(p.get("auth") == "needed" for p in parts) else (
            "error" if any(p.get("auth") == "error" for p in parts) else "ok"
        )
        error = next((p.get("error") for p in parts if p.get("error")), None)
        active = sum(p.get("orders_active", 0) for p in parts if p.get("auth") == "ok")
        last_poll = max((p.get("last_poll", "") for p in parts), default="")
        return {"auth": auth, "orders_active": active, "last_poll": last_poll, "error": error}
    return state.get(platform, {"auth": "unknown", "error": None})


@app.get("/")
async def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/api/state")
async def api_state():
    stats = db.today_stats()
    endpoints = load_endpoints()
    platforms = {}
    for p, cfg in config.PLATFORMS.items():
        ep = endpoints.get(p, {})
        if p == "demaecan":
            stores = [
                {**a, "uuid": key}
                for key, a in account_configs().items()
            ]
        else:
            stores = ep.get("stores", [])
        stores = db.per_store_stats(p, stores)
        platforms[p] = {
            "label": cfg["label"],
            "state": platform_state(p),
            "stats": stats.get(p, {"orders": 0, "revenue": 0.0}),
            "status_counts": db.status_counts(p),
            "stores": stores,
        }
    delivery = {}
    for acct in account_configs():
        delivery[acct] = db.get_delivery_time(account_session_key(acct))
    return {
        "platforms": platforms,
        "delivery_time": delivery,
    }


@app.get("/api/orders")
async def api_orders(platform: str = None, limit: int = 500, today: bool = True):
    return db.list_orders(platform=platform, limit=limit, today_only=today)


@app.get("/api/events")
async def api_events(request: Request):
    async def gen():
        q = pollers.subscribe()
        try:
            yield "retry: 15000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            pollers.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/delivery-time")
async def api_set_delivery_time(request: Request, store: str = "narimasu"):
    if store not in account_configs():
        return JSONResponse({"ok": False, "error": f"unknown store: {store}"}, status_code=400)
    body = await request.json()
    minutes = int(body.get("minutes"))
    if not (5 <= minutes <= 240):
        return JSONResponse({"ok": False, "error": "minutes must be 5-240"}, status_code=400)
    adapter = DemaeCanAdapter(account=store)
    try:
        result = await adapter.set_delivery_time(minutes)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    finally:
        await adapter.close()
    db.set_delivery_time(account_session_key(store), {"minutes": minutes, "set_by_dashboard": True})
    await pollers.broadcast(
        {"type": "delivery_time", "platform": "demaecan", "account": store, "value": {"minutes": minutes}}
    )
    return {"ok": True, **result}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8787, reload=False)

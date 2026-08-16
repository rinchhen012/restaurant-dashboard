"""Uber Eats merchant portal adapter (unofficial).

Uses the same internal API the manager portal web app calls:
  POST /manager/api/getActiveOrders   -> live orders
  POST /manager/api/getHistoricOrders -> past orders (today = completed history)
Store IDs / request templates were captured from a real session.
"""
import asyncio
import re
from datetime import datetime

from app import db
from app.adapters.base import Adapter, pick, to_float

BASE = "https://merchants.ubereats.com/manager/api"

# GraphQL live order state -> dashboard status
STATUS_MAP = {
    "CREATED": "NEW",
    "SCHEDULED": "NEW",
    "ACCEPTED": "ACCEPTED",
    "RELEASED": "PREPARING",
    "READY": "READY",
    "PICKED_UP": "PICKED_UP",
    "DELIVERED": "COMPLETED",
    "COMPLETED": "COMPLETED",
    "CANCELLED": "CANCELLED",
    "REJECTED": "REJECTED",
}

# Default body templates captured from the merchant portal (2026-08).
# Stored in data/endpoints.json if they ever need changing.
DEFAULT_ACTIVE_BODY = {
    "filters": {
        "currentTab": "activeOrders",
        "displayCurrencyCode": "JPY",
        "isEatsPassSubscriber": False,
        "locationConstraints": {"cities": [], "countries": [], "locationUuids": []},
        "orderIssuesV2": [],
        "issueOrderStatusFilter": [],
        "search": "",
        "displayByocIssues": False,
    },
    "pagination": {"limit": 50, "cursor": "", "nextTable": ""},
    "sort": {"sortColumn": None, "sortDirection": None},
}

DEFAULT_HISTORIC_BODY = {
    "filters": {
        "currentTab": "",
        "displayCurrencyCode": "",
        "locationConstraints": {"cities": [], "countries": [], "locationUuids": []},
        "dateFilter": {"startDate": None, "endDate": None, "lastUpdatedAt": ""},
        "isEatsPassSubscriber": False,
        "search": None,
        "orderIssuesV2": [],
        "issueOrderStatusFilter": [],
        "displayByocIssues": True,
    },
    "sort": {"sortColumn": "requestedAt", "sortDirection": "DESC"},
    "pagination": {"limit": 50, "cursor": "", "nextTable": ""},
}

# Past-date history requires BOTH pagingInfo and pagination keys together
# (captured from the portal's Custom date picker).
PAST_HISTORIC_BODY = {
    "filters": {
        "currentTab": "",
        "displayCurrencyCode": "",
        "locationConstraints": {"cities": [], "countries": [], "locationUuids": []},
        "dateFilter": {"startDate": None, "endDate": None, "lastUpdatedAt": ""},
        "isEatsPassSubscriber": False,
        "search": None,
        "orderIssuesV2": [],
        "issueOrderStatusFilter": [],
        "displayByocIssues": False,
    },
    "sort": {"sortColumn": "SORT_COLUMN_ORDER_COMPLETED_AT", "sortDirection": "SORT_DIRECTION_DESC"},
    "pagingInfo": {"cursor": "", "limit": 50, "nextTable": "liveOrders"},
    "pagination": {"cursor": "", "nextTable": "historyOrders", "limit": 50},
}


def parse_money(s: str) -> float | None:
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", s)
    if not m:
        return None
    return to_float(m.group(0).replace(",", ""))


def parse_american_time(s: str) -> str | None:
    """'8/16/2026, 3:13 PM' -> '2026-08-16T15:13:00'"""
    if not s:
        return None
    try:
        dt = datetime.strptime(s.strip(), "%m/%d/%Y, %I:%M %p")
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return s


class UberEatsAdapter(Adapter):
    platform = "ubereats"

    def extra_headers(self, cookie_dict: dict) -> dict:
        # The manager API requires the csrf header set to the sid cookie value.
        sid = cookie_dict.get("sid")
        return {"x-csrf-token": sid} if sid else {}

    def __init__(self):
        super().__init__()
        ep = self.endpoints
        self.active_body = {**DEFAULT_ACTIVE_BODY, **ep.get("active_orders_body", {})}
        self.historic_body = {**DEFAULT_HISTORIC_BODY, **ep.get("historic_orders_body", {})}
        if ep.get("location_uuids"):
            self.active_body["filters"]["locationConstraints"]["locationUuids"] = ep["location_uuids"]
            self.historic_body["filters"]["locationConstraints"]["locationUuids"] = ep["location_uuids"]

    def _parse_row(self, row: dict, source: str) -> dict:
        external_id = (
            row.get("orderUuid") or row.get("workflowUuid") or str(row.get("orderId")) or ""
        )
        status = "CANCELLED" if row.get("canceledBy") else ("COMPLETED" if source == "historic" else "NEW")
        restaurant = row.get("restaurant") or {}
        return {
            "external_id": external_id,
            "order_code": row.get("orderId") or "",
            "status": status,
            "status_raw": status,
            "items": [],
            "total": parse_money(row.get("salesTotal")),
            "currency": row.get("currencyCode") or "JPY",
            "placed_at": parse_american_time(row.get("requestedAt")),
            "customer": pick(row, "eater.name") or "",
            "courier": row.get("courierName") or "",
            "store_uuid": restaurant.get("uuid"),
            "store_name": restaurant.get("name") or "",
            "workflow_uuid": row.get("workflowUuid") or "",
        }

    async def fetch_live_status(self, order: dict) -> str | None:
        """Current order state from the portal's LiveOrderDetails op."""
        workflow_uuid = order.get("workflow_uuid") or ""
        restaurant_uuid = order.get("store_uuid") or ""
        if not workflow_uuid or not restaurant_uuid:
            return None
        query = """query LiveOrderDetails($workflowUUID: ID!, $metadata: Orders_OrderDetailsMetadataInput) {
  liveOrderDetails(workflowUUID: $workflowUUID, metadata: $metadata) {
    orderStateChanges {
      orderState
    }
  }
}"""
        body = {
            "operationName": "LiveOrderDetails",
            "variables": {
                "workflowUUID": workflow_uuid,
                "metadata": {"isEatsPassSubscriber": False, "locale": "en"},
                "detailsRequestedByRestaurantUUID": restaurant_uuid,
            },
            "query": query,
        }
        resp = await self._post_json("https://merchants.ubereats.com/manager/graphql?op=LiveOrderDetails", body)
        if resp.status_code >= 400:
            raise RuntimeError(f"{self.label}: LiveOrderDetails -> HTTP {resp.status_code}")
        payload = resp.json()
        changes = pick(payload, "data.liveOrderDetails.orderStateChanges") or []
        if not changes:
            return None
        last = changes[-1]
        state = last.get("orderState") if isinstance(last, dict) else None
        return STATUS_MAP.get(str(state)) if state else None

    async def fetch_order_items(self, order: dict) -> list[dict]:
        """Fetch item lines for one order.

        Live orders use the portal's GraphQL LiveOrderDetails op (the REST
        endpoint only knows orders that already have an orderUuid); past
        orders fall back to the REST getOrderDetails endpoint.
        """
        workflow_uuid = order.get("workflow_uuid") or ""
        restaurant_uuid = order.get("store_uuid") or ""
        items = await self._graphql_items(workflow_uuid, restaurant_uuid)
        if items is None:
            items = await self._rest_items(workflow_uuid, restaurant_uuid)
        return items or []

    async def _graphql_items(self, workflow_uuid: str, restaurant_uuid: str) -> list[dict] | None:
        query = """query LiveOrderDetails($workflowUUID: ID!, $metadata: Orders_OrderDetailsMetadataInput) {
  liveOrderDetails(workflowUUID: $workflowUUID, metadata: $metadata) {
    orderUUID
    items {
      name
      quantity
      price
      customizations {
        name
        options {
          name
          quantity
          price
        }
      }
    }
  }
}"""
        body = {
            "operationName": "LiveOrderDetails",
            "variables": {
                "workflowUUID": workflow_uuid,
                "metadata": {"isEatsPassSubscriber": False, "locale": "en"},
                "detailsRequestedByRestaurantUUID": restaurant_uuid,
            },
            "query": query,
        }
        resp = await self._post_json("https://merchants.ubereats.com/manager/graphql?op=LiveOrderDetails", body)
        if resp.status_code >= 400:
            raise RuntimeError(f"{self.label}: LiveOrderDetails -> HTTP {resp.status_code}")
        payload = resp.json()
        details = pick(payload, "data.liveOrderDetails")
        if not details:
            return None
        out = []
        for it in details.get("items") or []:
            out.append(self._item_with_options(it, "name"))
        return out

    def _item_with_options(self, it: dict, name_key: str) -> dict:
        options = []
        for cust in it.get("customizations") or []:
            for opt in cust.get("options") or []:
                options.append(
                    {
                        "title": opt.get(name_key, ""),
                        "quantity": opt.get("quantity", 1),
                        "price": parse_money(opt.get("price")),
                    }
                )
        return {
            "title": it.get(name_key, ""),
            "quantity": it.get("quantity", 1),
            "price": parse_money(it.get("price")),
            "options": options,
        }

    async def _rest_items(self, workflow_uuid: str, restaurant_uuid: str) -> list[dict] | None:
        resp = await self._post_json(
            f"{BASE}/getOrderDetails?localeCode=en",
            {"restaurantUuid": restaurant_uuid, "workflowUuid": workflow_uuid},
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"{self.label}: getOrderDetails -> HTTP {resp.status_code}")
        payload = resp.json()
        if payload.get("status") != "success":
            return None
        out = []
        for it in pick(payload, "data.items") or []:
            item = self._item_with_options(it, "title")
            item["title"] = it.get("title", "")
            out.append(item)
        return out

    async def fetch_orders_for_date(self, date: str) -> list[dict]:
        """All orders for a specific date, enriched with items. Past dates need
        the portal's combined pagingInfo+pagination body; today uses the
        standard one."""
        today = datetime.now().strftime("%Y-%m-%d")
        if date == today:
            body = json_dup(self.historic_body)
        else:
            body = json_dup(PAST_HISTORIC_BODY)
            body["filters"]["locationConstraints"]["locationUuids"] = self.endpoints.get("location_uuids", [])
        body["filters"]["dateFilter"]["startDate"] = f"{date} 00:00:00"
        body["filters"]["dateFilter"]["endDate"] = f"{date} 23:59:59"
        rows = await self._historic_pages(body)
        parsed = [self._parse_row(r, "historic") for r in rows]
        parsed = [o for o in parsed if o["external_id"]]
        sem = asyncio.Semaphore(10)

        async def enrich(o: dict) -> dict:
            async with sem:
                oid = o["external_id"]
                cached = db.get_cached_details(self.platform, oid)
                if cached:
                    o["items"] = cached[0]
                    return o
                stored = db.get_order_items(self.platform, oid)
                if stored and stored[0]:
                    o["items"] = stored[0]
                    return o
                try:
                    items = await self.fetch_order_items(o)
                    if items:
                        o["items"] = items
                    db.save_cached_details(self.platform, oid, o.get("items") or [], None)
                except Exception:
                    pass
            return o

        return list(await asyncio.gather(*[enrich(o) for o in parsed]))

    async def _post(self, endpoint: str, body: dict) -> dict:
        url = f"{BASE}/{endpoint}?localeCode=en"
        resp = await self._post_json(url, body)
        if resp.status_code >= 400:
            raise RuntimeError(f"{self.label}: {endpoint} -> HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def _historic_pages(self, body: dict) -> list[dict]:
        """Fetch all historic orders, following cursor pagination."""
        limit = (body.get("pagination") or {}).get("limit", 50)
        all_rows: list[dict] = []
        for _ in range(10):  # safety cap (10 pages x limit)
            resp = await self._post("getHistoricOrders", body)
            data = pick(resp, "data") or {}
            rows = data.get("orders") or []
            all_rows.extend(rows)
            pr = data.get("paginationResult") or {}
            cursor = pr.get("nextCursor") or ""
            if not rows or len(rows) < limit or not cursor:
                break
            body["pagination"]["cursor"] = cursor
        return all_rows

    async def fetch_orders(self) -> list[dict]:
        active = await self._post("getActiveOrders", self.active_body)
        rows = []
        for row in pick(active, "data.rows") or []:
            rows.append(self._parse_row(row, "active"))
        for row in await self._historic_pages(self._historic_body_today()):
            rows.append(self._parse_row(row, "historic"))
        return [r for r in rows if r["external_id"]]

    def _historic_body_today(self) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        body = json_dup(self.historic_body)
        body["filters"]["dateFilter"]["startDate"] = f"{today} 00:00:00"
        body["filters"]["dateFilter"]["endDate"] = f"{today} 23:59:59"
        return body

    async def fetch_delivery_time(self) -> dict | None:
        return None

    async def set_delivery_time(self, minutes: int) -> dict:
        raise NotImplementedError("Uber Eats has no store-side delivery time setting via portal")


def json_dup(obj):
    import copy

    return copy.deepcopy(obj)

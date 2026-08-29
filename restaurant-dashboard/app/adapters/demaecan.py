"""Demae-Can merchant admin adapter (unofficial).

Uses the partner portal's internal REST API (captured from a real session):
  POST /merchant-admin/api/v2/order/search/order            -> order list for a date range
  GET  /merchant-admin/api/v2/order/order-detail/{id}       -> items + order metadata
  GET  /merchant-admin/api/v1/shop/temporary-waiting-time   -> current waiting time
  POST /merchant-admin/api/v1/shop/temporary-waiting-time   -> set temporary waiting time

All responses wrap data as {"code": "MSA0000", "data": {...}}.

Multi-account: each store has its own login; pass `account` (config key)
and the adapter uses that account's session + shop id.
"""
import asyncio
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import httpx

from app import db
from app.adapters.base import Adapter, NeedsReauth, pick, to_float

BASE = "https://partner.demae-can.com/merchant-admin/api"

JA_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_translation_cache: dict[str, str] = {}
_geocode_cache: dict[str, tuple[float, float] | None] = {}
_route_cache: dict[tuple[str, str], tuple[float, list] | None] = {}


async def geocode(query: str) -> tuple[float, float] | None:
    """Geocode a Japanese address via CSIS (Univ. of Tokyo); cached, rate-limited."""
    if query in _geocode_cache:
        return _geocode_cache[query]
    for attempt in range(2):  # one retry for transient throttling
        await asyncio.sleep(1.1)  # CSIS: 1 req/sec limit
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(
                    "https://geocode.csis.u-tokyo.ac.jp/cgi-bin/simple_geocode.cgi",
                    params={"addr": query, "charset": "UTF8"},
                )
                root = ET.fromstring(r.text)
                lon_el = root.find(".//candidate/longitude")
                lat_el = root.find(".//candidate/latitude")
                if lon_el is not None and lat_el is not None and lon_el.text and lat_el.text:
                    lat, lon = float(lat_el.text), float(lon_el.text)
                    _geocode_cache[query] = (lat, lon)
                    return lat, lon
        except Exception:
            pass
        if attempt == 0:
            await asyncio.sleep(3)
    _geocode_cache[query] = None
    return None


async def bike_route(from_address: str, to_address: str) -> tuple[float, list] | None:
    """Bicycle route: (distance_km, geometry coords [[lon, lat], ...]) via OSRM bike profile."""
    key = (from_address, to_address)
    if key in _route_cache:
        return _route_cache[key]
    origin = await geocode(from_address)
    dest = await geocode(to_address)
    if not origin or not dest:
        _route_cache[key] = None
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://router.project-osrm.org/route/v1/bike/{origin[1]},{origin[0]};{dest[1]},{dest[0]}",
                params={"overview": "simplified", "geometries": "geojson"},
            )
            data = r.json()
            route = data["routes"][0]
            dist = round(route["distance"] / 1000, 1)
            coords = route["geometry"]["coordinates"]
            result = (dist, coords)
            _route_cache[key] = result
            return result
    except Exception:
        _route_cache[key] = None
        return None

# payment name -> (short English label, is cash)
def map_payment(name: str) -> tuple[str, bool]:
    n = unicodedata.normalize("NFKC", name or "").strip().lower()
    if any(k in n for k in ("代金引換", "代引き", "現金払い", "現金", "着払い")):
        return "Cash", True
    if "カード" in n or "クレジット" in n:
        return "Card", False
    if "d払い" in n:
        return "d Payment", False
    if "paypay" in n:
        return "PayPay", False
    if "楽天" in n:
        return "Rakuten Pay", False
    if "aupay" in n or "au" in n:
        return "au PAY", False
    if "line" in n:
        return "LINE Pay", False
    if "amazon" in n:
        return "Amazon Pay", False
    if "コンビニ" in n:
        return "Convenience Store", False
    if "メルペイ" in n or "merpay" in n:
        return "Mercari Pay", False
    if "ゆうちょ" in n or "郵便" in n:
        return "Japan Post Pay", False
    return name or "", False

_UNIT_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff々ー・]+?(?:県|市|区|町|村|丁目|番地|丁|番|号)")
_POSTAL_RE = re.compile(r"〒\d{3}-?\d{4}")
_NUM_RE = re.compile(r"[0-9０-９]+(?:[-－][0-9０-９]+)*")
_KANA_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff々ー・-]+")


def _address_segments(addr: str) -> list[str]:
    """Split a Japanese address into ordered units (prefecture/city/district/
    numbers/building), preserving the original reading order."""
    tokens: list[str] = []
    i, n = 0, len(addr)
    while i < n:
        m = _POSTAL_RE.match(addr, i) or _UNIT_RE.match(addr, i)
        if m:
            tokens.append(m.group(0))
            i = m.end()
            continue
        m = _NUM_RE.match(addr, i)
        if m:
            tokens.append(m.group(0))
            i = m.end()
            continue
        m = _KANA_RE.match(addr, i)
        if m:
            tokens.append(m.group(0))
            i = m.end()
            continue
        i += 1
    return tokens


async def _google_translate(text: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": "ja", "tl": "en", "dt": "t", "q": text},
            )
            data = r.json()
            parts = data[0] if isinstance(data, list) else []
            out = "".join(p[0] for p in parts if p and p[0])
            return out.strip() or None
    except Exception:
        return None


async def _mymemory_translate(text: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.mymemory.translated.net/get",
                params={"q": text, "langpair": "ja|en"},
            )
            data = r.json()
            translated = (data.get("responseData") or {}).get("translatedText") or ""
            if translated and translated.upper() != text.upper():
                return translated
    except Exception:
        pass
    return None


async def translate_ja_en(text: str) -> str | None:
    """Translate Japanese -> English (Google endpoint, MyMemory fallback, cached)."""
    if not text or not JA_RE.search(text):
        return None
    if text in _translation_cache:
        return _translation_cache[text] or None
    translated = await _google_translate(text) or await _mymemory_translate(text)
    _translation_cache[text] = translated or ""
    return translated


async def translate_address_ordered(address: str) -> str | None:
    """Translate a full Japanese address to English.

    Uses Google's whole-address translation, which reads place names
    correctly (春日町 -> Kasugacho, not "Kasuga Town"; 豊玉中 -> Toyamaka,
    not "Junior High School"). The output uses standard English address
    order. Falls back to segment-ordered translation if the whole-address
    call fails.
    """
    if not address or not JA_RE.search(address):
        return None
    if address in _translation_cache:
        return _translation_cache[address] or None
    result = await _google_translate(address)
    if not result:
        result = await _translate_segments(address)
    _translation_cache[address] = result or ""
    return result


async def _translate_segments(address: str) -> str | None:
    """Segment-ordered translation (kept as a fallback)."""
    if not address or not JA_RE.search(address):
        return None
    out = []
    for seg in _address_segments(address):
        if seg.startswith("〒") or (seg and seg[0].isdigit()):
            out.append(seg)
        else:
            out.append((await translate_ja_en(seg)) or seg)
    result = " ".join(out).strip()
    _translation_cache[address] = result
    return result or None


def account_configs() -> dict[str, dict]:
    """demaecan account list from endpoints.json: {account_key: {...}}."""
    from app import config

    ep = config.load_json(config.DATA_DIR / "endpoints.json", default={}).get("demaecan", {})
    accounts = ep.get("accounts")
    if accounts:
        return accounts
    return {
        "narimasu": {
            "shop_id": ep.get("shop_id"),
            "order_type": ep.get("order_type", "0"),
            "label": "Narimasu Indian",
            "name": "インド・ネパール・レストラン＆バー　ナマステ",
        }
    }


def account_session_key(account: str) -> str:
    return "demaecan" if account == "narimasu" else f"demaecan-{account}"


class DemaeCanAdapter(Adapter):
    platform = "demaecan"

    def __init__(self, account: str = "narimasu"):
        self.account = account
        acct = account_configs().get(account)
        if not acct:
            raise ValueError(f"unknown demaecan account: {account}")
        self.shop_id = acct.get("shop_id")
        self.order_type = acct.get("order_type", "0")
        self.shop_address = acct.get("shop_address")
        self.store_label = acct.get("label", account)
        self.store_name = acct.get("name", "")
        super().__init__()

    @property
    def label(self) -> str:
        return f"Demae-Can ({self.store_label})"

    def client(self):
        if self._client is None:
            cookies = db.load_session(account_session_key(self.account))
            if not cookies:
                raise NeedsReauth(f"{self.label}: no saved session")
            cookie_dict = {c["name"]: c["value"] for c in cookies}
            self._client = self._make_client(cookie_dict)
        return self._client

    # ---------- orders ----------

    def _order_search_body(self) -> dict:
        fmt = "%Y-%m-%dT%H:%M:%S+09:00"
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "orderPickId": "",
            "memberInfo": "",
            "orderDatetimeFrom": today.strftime(fmt),
            "orderDatetimeTo": today.replace(hour=23, minute=59, second=59).strftime(fmt),
            "deliveryDatetimeFrom": "",
            "deliveryDatetimeTo": "",
            "orderSiteType": "ALL",
            "orderChangeTypeList": [],
            "causeType": "NONE",
            "itemCostCompensationType": "NONE",
            "shopIdList": [self.shop_id] if self.shop_id else [],
            "offset": 0,
            "limit": 50,
        }

    def _parse_order(self, o: dict) -> dict:
        placed_at = pick(o, "orderDatetime", "createdAt")
        if placed_at:
            placed_at = str(placed_at)[:19]
        change = pick(o, "orderChangeType")
        return {
            "external_id": str(pick(o, "orderId", "orderNo")),
            "status": "CHANGED" if change else "NEW",
            "status_raw": str(change) if change else "NEW",
            "items": [],
            "total": to_float(pick(o, "invoiceAmount", "shopSalesAmount", "totalAmount", "orderAmount")),
            "currency": "JPY",
            "placed_at": placed_at,
            "customer": pick(o, "ordererName") or "",
            "delivery_datetime": str(pick(o, "deliveryDatetime") or "")[:19],
            "store_uuid": self.account,
            "store_name": self.store_name,
        }

    async def _search_pages(self, body: dict) -> list[dict]:
        """Fetch all orders matching a search body, following offset pagination."""
        out: list[dict] = []
        for _ in range(10):  # safety cap (10 pages x limit)
            resp = await self._post_json(f"{BASE}/v2/order/search/order", body)
            if resp.status_code >= 400:
                raise RuntimeError(f"{self.label}: order search -> HTTP {resp.status_code}: {resp.text[:300]}")
            payload = resp.json()
            if payload.get("code") != "MSA0000":
                raise RuntimeError(f"{self.label}: order search failed: {payload.get('code')} {payload.get('message', '')}")
            orders = pick(payload, "data.searchOrderList") or []
            out.extend(orders)
            limit = body.get("limit", 50)
            if len(orders) < limit:
                break
            body["offset"] = body.get("offset", 0) + limit
        return out

    async def fetch_orders(self) -> list[dict]:
        orders = await self._search_pages(self._order_search_body())
        parsed = [self._parse_order(o) for o in orders]
        return [o for o in parsed if o["external_id"]]

    async def fetch_order_details(self, order: dict) -> tuple[list[dict], dict]:
        """Full item lines + order metadata (remarks, times, payment,
        customer info) for one order."""
        oid = order.get("external_id")
        if not oid:
            return [], {}
        payload = await self._get_json(f"{BASE}/v2/order/order-detail/{oid}")
        if not payload or payload.get("code") != "MSA0000":
            return [], {}
        data = pick(payload, "data") or {}
        orderer = data.get("orderer") or {}
        order_info = data.get("order") or {}
        remarks = data.get("remarks")
        if isinstance(remarks, list):
            remarks = " / ".join(
                str(r.get("remarkText") or r.get("remarkName") or r) for r in remarks if r
            )
        customer = orderer.get("ordererName") or ""
        address = orderer.get("address") or ""
        remarks = data.get("remarks")
        if isinstance(remarks, list):
            remarks = " / ".join(
                str(r.get("remarkText") or r.get("remarkName") or r) for r in remarks if r
            )
        customer_en = await translate_ja_en(customer) if customer else None
        address_en = await translate_address_ordered(address) if address else None
        remarks_en = await translate_ja_en(remarks) if isinstance(remarks, str) and remarks else None
        payment_short, payment_cash = map_payment(order_info.get("paymentName") or "")
        receipt = order_info.get("receiptAddress")
        receipt = receipt.strip() if isinstance(receipt, str) and receipt.strip() else ""
        distance_km = None
        route = None
        store_lat = store_lon = cust_lat = cust_lon = None
        if self.shop_address and address:
            geo_address = re.sub(r"〒\d{3}-?\d{4}\s*", "", address).strip()
            result = await bike_route(self.shop_address, geo_address)
            if result is None and " " in geo_address:
                # retry without the trailing building/room part
                result = await bike_route(self.shop_address, geo_address.rsplit(" ", 1)[0])
            if result:
                distance_km, route = result
                sp = await geocode(self.shop_address)
                cp = await geocode(geo_address)
                if sp:
                    store_lat, store_lon = sp
                if cp:
                    cust_lat, cust_lon = cp
        meta = {
            "customer": customer,
            "customer_en": customer_en,
            "order_time": str(order_info.get("orderDatetime") or "")[:19],
            "delivery_time": str(order_info.get("deliveryDatetime") or "")[:19],
            "wait_time": order_info.get("waitTime"),
            "payment": payment_short,
            "payment_cash": payment_cash,
            "past_orders": order_info.get("orderCountByShop"),
            "address": address,
            "address_en": address_en,
            "distance_km": distance_km,
            "route": route,
            "store_lat": store_lat,
            "store_lon": store_lon,
            "cust_lat": cust_lat,
            "cust_lon": cust_lon,
            "phone": orderer.get("phoneNo") or "",
            "receipt": receipt,
            "remarks": remarks if isinstance(remarks, str) and remarks else "",
            "remarks_en": remarks_en,
        }
        item_list = pick(data, "orderItem.itemList") or []
        out: list[dict] = []

        def add(i: dict):
            options = [
                {
                    "title": o.get("optionName", ""),
                    "quantity": o.get("optionOrderCount", 1),
                    "price": to_float(o.get("optionTotalPrice")),
                }
                for o in i.get("optionList") or []
            ]
            out.append(
                {
                    "title": i.get("itemName", ""),
                    "quantity": i.get("itemOrderCount", 1),
                    "price": to_float(i.get("itemTotalPrice")),
                    "options": options,
                }
            )
            for c in i.get("childItemList") or []:
                add(c)

        for i in item_list:
            add(i)
        return out, meta

    async def fetch_orders_for_date(self, date: str) -> list[dict]:
        """All orders for a specific (past) date, enriched with items + meta."""
        body = self._order_search_body()
        body["orderDatetimeFrom"] = f"{date}T00:00:00+09:00"
        body["orderDatetimeTo"] = f"{date}T23:59:59+09:00"
        orders = await self._search_pages(body)
        parsed = [self._parse_order(o) for o in orders if o.get("orderId")]
        sem = asyncio.Semaphore(5)

        async def enrich(o: dict) -> dict:
            async with sem:
                oid = o["external_id"]
                cached = db.get_cached_details(self.platform, oid)
                if cached:
                    o["items"], o["meta"] = cached
                    return o
                stored = db.get_order_items(self.platform, oid)
                if stored and (stored[0] or stored[1]):
                    o["items"], o["meta"] = stored
                    return o
                try:
                    items, meta = await self.fetch_order_details(o)
                    if items or meta:
                        o["items"], o["meta"] = items, meta
                    db.save_cached_details(self.platform, oid, o.get("items") or [], o.get("meta"))
                except Exception:
                    pass
            return o

        return list(await asyncio.gather(*[enrich(o) for o in parsed]))

    async def fetch_order_items(self, order: dict) -> list[dict]:
        items, _ = await self.fetch_order_details(order)
        return items

    # ---------- delivery time ----------

    async def _shop_context(self) -> tuple[int, str]:
        """Fetch shopId + orderType from the status list if not configured."""
        if self.shop_id:
            return self.shop_id, self.order_type
        resp = await self._post_json(f"{BASE}/v1/shop/status-list", {"shopIdList": []})
        payload = resp.json()
        shop = (pick(payload, "data.shopList") or [{}])[0]
        return shop.get("shopId"), shop.get("orderType", "0")

    async def fetch_delivery_time(self) -> dict | None:
        url = f"{BASE}/v1/shop/temporary-waiting-time"
        payload = await self._get_json(url)
        if not payload or payload.get("code") != "MSA0000":
            raise RuntimeError(f"{self.label}: waiting time GET failed: {payload}")
        shop = (pick(payload, "data.shopList") or [{}])[0]
        standard = shop.get("standardWaitingTime")
        temp = None
        for t in shop.get("temporaryWaitingTimeList") or []:
            if t.get("isCurrentApplying"):
                temp = t
                break
        minutes = (temp or {}).get("temporaryWaitingTime") if temp else None
        if minutes is None:
            minutes = standard
        return {
            "standard_minutes": standard,
            "minutes": minutes,
            "temporary": temp or None,
            "raw": shop,
        }

    async def set_delivery_time(self, minutes: int) -> dict:
        url = f"{BASE}/v1/shop/temporary-waiting-time"
        shop_id, order_type = await self._shop_context()
        now = datetime.utcnow().replace(microsecond=0)
        end = now + timedelta(hours=2)
        body = {
            "shopList": [{"shopId": shop_id, "orderType": order_type}],
            "temporaryWaitingTime": int(minutes),
            "applyStartType": "1",
            "applyStartTime": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "applyEndType": "2",
            "applyEndTime": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        resp = await self._post_json(url, body)
        if resp.status_code >= 400:
            raise RuntimeError(f"{self.label}: set waiting time -> HTTP {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        if payload.get("code") != "MSA0000":
            raise RuntimeError(f"{self.label}: set waiting time failed: {payload.get('code')} {payload.get('message', '')}")
        return {"ok": True, "response": payload}

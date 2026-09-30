"""Base adapter: session-cookie HTTP client + order normalization."""
import json
from abc import ABC, abstractmethod

import httpx

from app import config
from app import db


class NeedsReauth(Exception):
    """Session expired or invalid — user must re-run session capture."""


class Adapter(ABC):
    platform: str
    api_base: str = ""

    def __init__(self):
        self.endpoints = config.load_json(config.DATA_DIR / "endpoints.json", default={}).get(
            self.platform, {}
        )
        self._client: httpx.AsyncClient | None = None

    @property
    def label(self) -> str:
        return config.PLATFORMS[self.platform]["label"]

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            cookies = db.load_session(self.platform)
            if not cookies:
                raise NeedsReauth(f"{self.label}: no saved session")
            cookie_dict = {c["name"]: c["value"] for c in cookies}
            self._client = self._make_client(cookie_dict)
        return self._client

    def _make_client(self, cookie_dict: dict) -> httpx.AsyncClient:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        }
        extra = self.extra_headers(cookie_dict)
        if extra:
            headers.update(extra)
        return httpx.AsyncClient(
            cookies=cookie_dict,
            timeout=30.0,
            follow_redirects=True,
            headers=headers,
        )

    def extra_headers(self, cookie_dict: dict) -> dict:
        return {}

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get_json(self, url: str, **kwargs):
        resp = await self.client().get(url, **kwargs)
        if resp.status_code in (401, 403):
            await self.close()
            raise NeedsReauth(f"{self.label}: session invalid (HTTP {resp.status_code})")
        if resp.status_code == 200:
            try:
                return resp.json()
            except Exception:
                return None
        raise RuntimeError(f"{self.label}: GET {url} -> HTTP {resp.status_code}")

    async def _post_json(self, url: str, payload: dict):
        resp = await self.client().post(url, json=payload)
        if resp.status_code in (401, 403):
            await self.close()
            raise NeedsReauth(f"{self.label}: session invalid (HTTP {resp.status_code})")
        return resp

    @abstractmethod
    async def fetch_orders(self) -> list[dict]:
        """Return a list of normalized order dicts."""

    @abstractmethod
    async def fetch_delivery_time(self) -> dict | None:
        """Return current delivery/waiting time info or None."""

    @abstractmethod
    async def set_delivery_time(self, minutes: int) -> dict:
        """Set temporary delivery/waiting time in minutes."""


def pick(d: dict, *keys):
    """First non-None value among keys (supports dotted paths)."""
    for k in keys:
        node = d
        ok = True
        for part in k.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                ok = False
                break
        if ok and node is not None:
            return node
    return None


def as_list(node) -> list:
    if isinstance(node, list):
        return node
    if isinstance(node, dict):
        for k in ("orders", "items", "data", "results", "orderItems", "history"):
            v = node.get(k)
            if isinstance(v, list):
                return v
    return []


def to_float(v) -> float | None:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None

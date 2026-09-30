"""Analyze captured traffic and write adapter endpoint config.

Usage:
    python -m app.discovery

Reads data/captures/{platform}_requests.json, scores candidate API
endpoints, and writes data/endpoints.json which the adapters use.
Auto-picks best candidates but leaves the file editable so you can
fix mappings after reviewing the printout.
"""
import json
import re
from pathlib import Path

from app import config

CATEGORIES = {
    "orders": {"hints": ("order",), "method": "GET"},
    "order_detail": {"hints": ("order",), "method": "GET", "detail": True},
    "delivery_time_get": {"hints": ("waiting", "delivery", "delivery-time", "order-accept", "accept"), "method": "GET"},
    "delivery_time_set": {"hints": ("waiting", "delivery", "delivery-time", "order-accept", "accept"), "method": "POST"},
    "business_hours": {"hints": ("business", "hours", "open"), "method": "GET"},
}


def path_pattern(url: str) -> str:
    path = url.split("?", 1)[0]
    path = re.sub(r"/[0-9a-f]{8,}|/\d+", "/{id}", path)
    return path


def analyze(platform: str) -> dict:
    reqs = config.load_json(config.CAPTURES_DIR / f"{platform}_requests.json", default=[])
    if not reqs:
        print(f"No captures for {platform}. Run: python -m app.capture {platform}")
        return {}

    grouped: dict[str, list[dict]] = {}
    for r in reqs:
        pat = f"{r['method']} {path_pattern(r['url'])}"
        grouped.setdefault(pat, []).append(r)

    # Score each pattern against each category.
    best = {}
    for name, spec in CATEGORIES.items():
        candidates = []
        for pat, hits in grouped.items():
            method, _, url_pattern = pat.partition(" ")
            if method != spec["method"]:
                continue
            score = sum(h in pat.lower() for h in spec["hints"])
            if spec.get("detail") and "{id}" not in url_pattern:
                score -= 1
            elif not spec.get("detail") and "{id}" in url_pattern:
                score -= 1
            if score <= 0:
                continue
            sample = hits[-1]
            candidates.append((score, pat, sample))
        candidates.sort(key=lambda c: (-c[0], c[1]))
        if candidates:
            best[name] = candidates[0]

    print(f"\n=== {platform} endpoint candidates ===")
    for name, (score, pat, sample) in best.items():
        body = (sample.get("response_body") or "")[:300]
        print(f"\n[{name}] score={score}\n  {pat}\n  sample: {body!r}")

    return {name: pat for name, (_, pat, _) in best.items()}


def main():
    endpoints = config.load_json(config.DATA_DIR / "endpoints.json", default={})
    for platform in config.PLATFORMS:
        found = analyze(platform)
        endpoints.setdefault(platform, {})
        for k, v in found.items():
            endpoints[platform].setdefault(k, v)
    out = config.DATA_DIR / "endpoints.json"
    config.save_json(out, endpoints)
    print(f"\nWrote {out}")
    print("Review/edit data/endpoints.json if any candidate looks wrong.")


if __name__ == "__main__":
    main()

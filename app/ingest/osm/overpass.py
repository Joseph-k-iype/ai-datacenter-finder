"""Thin Overpass client with retry + backoff + local caching."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import PROJECT_ROOT, get_settings
from app.core.logging import get_logger

log = get_logger("ingest.osm.overpass")

CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "overpass"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_key(query: str) -> Path:
    h = hashlib.sha256(query.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{h}.json"


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=5, min=5, max=120),
    reraise=True,
)
def _post(query: str) -> dict[str, Any]:
    settings = get_settings()
    log.info("overpass.request", url=settings.overpass_url, bytes=len(query))
    resp = requests.post(
        settings.overpass_url,
        data={"data": query},
        timeout=(30, settings.overpass_timeout + 30),
        headers={
            "User-Agent": settings.overpass_user_agent,
            "Accept": "application/json",
        },
    )
    if resp.status_code == 429 or resp.status_code >= 500:
        log.warning("overpass.retryable", status=resp.status_code)
        resp.raise_for_status()
    resp.raise_for_status()
    return resp.json()


def fetch(query: str, use_cache: bool = True) -> dict[str, Any]:
    """Run an Overpass QL query (POST). Cached on disk by query hash."""
    settings = get_settings()
    cache = _cache_key(query)
    if use_cache and cache.exists():
        log.info("overpass.cache.hit", path=str(cache))
        return json.loads(cache.read_text())

    rendered = query.replace("{timeout}", str(settings.overpass_timeout))
    data = _post(rendered)
    cache.write_text(json.dumps(data))
    # Be polite — Overpass rate limit.
    time.sleep(settings.overpass_rate_limit_sec)
    return data

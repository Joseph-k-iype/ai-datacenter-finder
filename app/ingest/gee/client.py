"""ee.Initialize wrapper. Service-account preferred; falls back to user creds."""
from __future__ import annotations

import os
from functools import lru_cache

import ee

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger("ingest.gee.client")


@lru_cache(maxsize=1)
def init_ee() -> None:
    settings = get_settings()
    if not settings.gee_project:
        raise RuntimeError("GEE_PROJECT must be set (.env)")

    sa_json = settings.gee_service_account_json
    if sa_json and os.path.exists(sa_json):
        # Service account auth
        import json as _json

        with open(sa_json) as f:
            payload = _json.load(f)
        credentials = ee.ServiceAccountCredentials(payload.get("client_email"), sa_json)
        ee.Initialize(credentials, project=settings.gee_project)
        log.info("gee.init.service_account", project=settings.gee_project)
    else:
        ee.Initialize(project=settings.gee_project)
        log.info("gee.init.user_credentials", project=settings.gee_project)

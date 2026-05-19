"""ee.Initialize wrapper. Service-account preferred; falls back to user creds."""
from __future__ import annotations

import os
from functools import lru_cache

import ee
from ee import ee_exception, oauth

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger("ingest.gee.client")


@lru_cache(maxsize=1)
def init_ee() -> None:
    settings = get_settings()
    if not settings.gee_project or settings.gee_project == "your-gcp-project-id":
        raise RuntimeError(
            "GEE_PROJECT must be set in .env to a real Google Cloud project ID "
            "(not your email address). The project must have Earth Engine enabled."
        )

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
        credentials_path = oauth.get_credentials_path()
        if not os.path.exists(credentials_path):
            raise RuntimeError(
                "Google Earth Engine user credentials are missing. Run "
                "`make gee-auth`, sign in with your Earth Engine-enabled Google account, "
                "then retry this command."
            )
        try:
            ee.Initialize(project=settings.gee_project)
        except ee_exception.EEException as exc:
            raise RuntimeError(
                "Google Earth Engine initialization failed. Re-run `make gee-auth`, "
                "verify your Google account has Earth Engine access, and confirm "
                "GEE_PROJECT in .env is a real Earth Engine-enabled Google Cloud project ID."
            ) from exc
        log.info("gee.init.user_credentials", project=settings.gee_project)

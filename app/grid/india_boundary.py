"""Load GADM India L1 (states) MultiPolygons into Postgres.

GADM Free for academic / non-commercial use. The L1 file has 36 states/UTs.
Download is cached at data/raw/gadm41_IND_1.json.
"""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import requests
from shapely.geometry import MultiPolygon, shape
from sqlalchemy import text

from app.core.config import PROJECT_ROOT, load_pipeline_config
from app.core.db import session_scope
from app.core.logging import get_logger

log = get_logger("grid.india_boundary")

CACHE_PATH = PROJECT_ROOT / "data" / "raw" / "gadm41_IND_1.json"

# Map GADM NAME_1 → 2-letter Indian state code (ISO 3166-2:IN minus the IN- prefix).
STATE_CODE_BY_NAME: dict[str, str] = {
    "Andaman and Nicobar": "AN",
    "Andhra Pradesh": "AP",
    "Arunachal Pradesh": "AR",
    "Assam": "AS",
    "Bihar": "BR",
    "Chandigarh": "CH",
    "Chhattisgarh": "CT",
    "Dadra and Nagar Haveli and Daman and Diu": "DH",
    "Delhi": "DL",
    "Goa": "GA",
    "Gujarat": "GJ",
    "Haryana": "HR",
    "Himachal Pradesh": "HP",
    "Jammu and Kashmir": "JK",
    "Jharkhand": "JH",
    "Karnataka": "KA",
    "Kerala": "KL",
    "Ladakh": "LA",
    "Lakshadweep": "LD",
    "Madhya Pradesh": "MP",
    "Maharashtra": "MH",
    "Manipur": "MN",
    "Meghalaya": "ML",
    "Mizoram": "MZ",
    "Nagaland": "NL",
    "Odisha": "OD",
    "Puducherry": "PY",
    "Punjab": "PB",
    "Rajasthan": "RJ",
    "Sikkim": "SK",
    "Tamil Nadu": "TN",
    "Telangana": "TG",
    "Tripura": "TR",
    "Uttar Pradesh": "UP",
    "Uttarakhand": "UT",
    "West Bengal": "WB",
}


def _download_gadm() -> Path:
    if CACHE_PATH.exists() and CACHE_PATH.stat().st_size > 1_000_000:
        return CACHE_PATH
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    url = load_pipeline_config()["india"]["gadm_url"]
    log.info("gadm.download.start", url=url)
    resp = requests.get(url, timeout=180)
    resp.raise_for_status()
    CACHE_PATH.write_bytes(resp.content)
    log.info("gadm.download.done", path=str(CACHE_PATH), bytes=len(resp.content))
    return CACHE_PATH


def load_states_gdf() -> gpd.GeoDataFrame:
    """Return a GeoDataFrame with columns: state_code, state_name, geom."""
    path = _download_gadm()
    raw = json.loads(path.read_text())
    rows = []
    for feat in raw["features"]:
        name = feat["properties"]["NAME_1"]
        code = STATE_CODE_BY_NAME.get(name, name[:2].upper())
        geom = shape(feat["geometry"])
        # Force MultiPolygon for table consistency.
        if geom.geom_type == "Polygon":
            geom = MultiPolygon([geom])
        rows.append({"state_code": code, "state_name": name, "geometry": geom})
    return gpd.GeoDataFrame(rows, crs="EPSG:4326")


def load_into_postgres() -> int:
    """Insert (replace) india_states. Returns row count."""
    gdf = load_states_gdf()
    with session_scope() as session:
        session.execute(text("TRUNCATE dc_india.india_states"))
        for _, row in gdf.iterrows():
            session.execute(
                text(
                    """
                    INSERT INTO dc_india.india_states (state_code, state_name, geom)
                    VALUES (:state_code, :state_name, ST_GeomFromText(:wkt, 4326))
                    """
                ),
                {
                    "state_code": row.state_code,
                    "state_name": row.state_name,
                    "wkt": row.geometry.wkt,
                },
            )
    log.info("india_states.loaded", n=len(gdf))
    return len(gdf)

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
from shapely.ops import unary_union
from sqlalchemy import text

from app.core.config import PROJECT_ROOT, load_pipeline_config
from app.core.db import session_scope
from app.core.logging import get_logger

log = get_logger("grid.india_boundary")

CACHE_PATH = PROJECT_ROOT / "data" / "raw" / "gadm41_IND_1.json"

# Map GADM NAME_1 → 2-letter Indian state code (ISO 3166-2:IN minus the IN- prefix).
STATE_CODE_BY_NAME: dict[str, str] = {
    "Andaman and Nicobar": "AN",
    "AndamanandNicobar": "AN",
    "Andhra Pradesh": "AP",
    "AndhraPradesh": "AP",
    "Arunachal Pradesh": "AR",
    "ArunachalPradesh": "AR",
    "Assam": "AS",
    "Bihar": "BR",
    "Chandigarh": "CH",
    "Chhattisgarh": "CT",
    "DadraandNagarHaveli": "DH",
    "Dadra and Nagar Haveli and Daman and Diu": "DH",
    "DamanandDiu": "DH",
    "Delhi": "DL",
    "NCTofDelhi": "DL",
    "Goa": "GA",
    "Gujarat": "GJ",
    "Haryana": "HR",
    "Himachal Pradesh": "HP",
    "HimachalPradesh": "HP",
    "Jammu and Kashmir": "JK",
    "JammuandKashmir": "JK",
    "Jharkhand": "JH",
    "Karnataka": "KA",
    "Kerala": "KL",
    "Ladakh": "LA",
    "Lakshadweep": "LD",
    "Madhya Pradesh": "MP",
    "MadhyaPradesh": "MP",
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
    "TamilNadu": "TN",
    "Telangana": "TG",
    "Tripura": "TR",
    "Uttar Pradesh": "UP",
    "UttarPradesh": "UP",
    "Uttarakhand": "UT",
    "West Bengal": "WB",
    "WestBengal": "WB",
}

STATE_NAME_BY_CODE: dict[str, str] = {
    "AN": "Andaman and Nicobar",
    "AP": "Andhra Pradesh",
    "AR": "Arunachal Pradesh",
    "AS": "Assam",
    "BR": "Bihar",
    "CH": "Chandigarh",
    "CT": "Chhattisgarh",
    "DH": "Dadra and Nagar Haveli and Daman and Diu",
    "DL": "Delhi",
    "GA": "Goa",
    "GJ": "Gujarat",
    "HR": "Haryana",
    "HP": "Himachal Pradesh",
    "JK": "Jammu and Kashmir",
    "JH": "Jharkhand",
    "KA": "Karnataka",
    "KL": "Kerala",
    "LA": "Ladakh",
    "LD": "Lakshadweep",
    "MP": "Madhya Pradesh",
    "MH": "Maharashtra",
    "MN": "Manipur",
    "ML": "Meghalaya",
    "MZ": "Mizoram",
    "NL": "Nagaland",
    "OD": "Odisha",
    "PY": "Puducherry",
    "PB": "Punjab",
    "RJ": "Rajasthan",
    "SK": "Sikkim",
    "TN": "Tamil Nadu",
    "TG": "Telangana",
    "TR": "Tripura",
    "UP": "Uttar Pradesh",
    "UT": "Uttarakhand",
    "WB": "West Bengal",
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
    by_code: dict[str, list] = {}
    for feat in raw["features"]:
        name = feat["properties"]["NAME_1"]
        code = STATE_CODE_BY_NAME.get(name)
        if code is None:
            raise ValueError(f"Unmapped GADM India state/UT name: {name!r}")
        geom = shape(feat["geometry"])
        by_code.setdefault(code, []).append(geom)

    rows = []
    for code, geoms in sorted(by_code.items()):
        geom = unary_union(geoms)
        # Force MultiPolygon for table consistency after dissolving duplicate GADM features.
        if geom.geom_type == "Polygon":
            geom = MultiPolygon([geom])
        if geom.geom_type != "MultiPolygon":
            raise ValueError(f"Unexpected geometry type for {code}: {geom.geom_type}")
        rows.append(
            {
                "state_code": code,
                "state_name": STATE_NAME_BY_CODE.get(code, code),
                "geometry": geom,
            }
        )
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

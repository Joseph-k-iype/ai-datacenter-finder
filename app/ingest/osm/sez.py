"""OSM Special Economic Zone ingest.

Pulls SEZ-tagged polygons from OSM via Overpass. The tag space here is
genuinely messy: India's SEZs are sometimes mapped with the canonical
``boundary=special_economic_zone`` relation, sometimes as ``landuse=
industrial`` parcels whose name contains "SEZ". We union the two.

Policy classification is derived from the name where possible:
  - 'data_center_incentive' if name mentions IT/data/cloud
  - 'it_services' for IT-named SEZs
  - 'multi_product' for the catch-all
This is heuristic but data-driven — when names change in OSM, the
classification updates with the next refresh; nothing to maintain.
"""
from __future__ import annotations

import re
from typing import Any

import pandas as pd
from shapely.geometry import Point, Polygon

from app.core.config import load_sources
from app.core.logging import get_logger
from app.governance.contracts import get_contract, schema_hash
from app.governance.lineage import ingestion_run, should_skip
from app.ingest.base import validate_and_split
from app.ingest.osm import overpass
from app.ingest.osm._writers import truncate

log = get_logger("ingest.osm.sez")


_IT_KEYWORDS = re.compile(r"\b(IT|software|tech|cyber|data|cloud|ITES|HITEC|software park)\b", re.I)
_DATA_CENTER_KEYWORDS = re.compile(r"\b(data ?cent(re|er)|cloud|hyperscaler)\b", re.I)


def _classify_policy(name: str | None) -> str | None:
    if not name:
        return None
    if _DATA_CENTER_KEYWORDS.search(name):
        return "data_center_incentive"
    if _IT_KEYWORDS.search(name):
        return "it_services"
    return "multi_product"


def _element_to_row(el: dict[str, Any]) -> dict[str, Any] | None:
    tags = el.get("tags", {}) or {}
    name = tags.get("name")

    # Geometry: prefer the inline geom (way) → polygon; otherwise the
    # Overpass "center" attribute (relation) → small buffer polygon.
    if el.get("type") == "way" and "geometry" in el:
        coords = [(pt["lon"], pt["lat"]) for pt in el["geometry"]]
        if len(coords) < 3:
            return None
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        try:
            polygon = Polygon(coords)
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
        except Exception:  # noqa: BLE001
            return None
        wkt_str = polygon.wkt
        centroid = polygon.centroid
    elif "center" in el:
        lon, lat = el["center"]["lon"], el["center"]["lat"]
        # ~500m buffer around the center as a placeholder polygon —
        # better than dropping the row entirely.
        polygon = Polygon(
            [
                (lon - 0.005, lat - 0.005),
                (lon + 0.005, lat - 0.005),
                (lon + 0.005, lat + 0.005),
                (lon - 0.005, lat + 0.005),
                (lon - 0.005, lat - 0.005),
            ]
        )
        wkt_str = polygon.wkt
        centroid = Point(lon, lat)
    else:
        return None

    return {
        "osm_id": el.get("id"),
        "name": name,
        "operator": tags.get("operator"),
        "policy_tag": _classify_policy(name),
        "wkt": wkt_str,
        "centroid_wkt": centroid.wkt,
    }


def ingest_sez(*, fresh: bool = False) -> int:
    if existing := should_skip("osm.sez", fresh=fresh):
        log.info("ingest.skip_recent", source="osm.sez", existing_run_id=str(existing))
        return 0

    sources = load_sources()
    q = sources["osm_overpass"]["sez"]["query"]
    contract = get_contract("osm.sez")

    with ingestion_run(
        source="osm.sez",
        upstream_source="overpass[boundary=special_economic_zone | landuse=industrial+SEZ]",
        schema_hash=schema_hash(contract),
    ) as run:
        truncate("raw_sez")
        raw = overpass.fetch(q)
        rows: list[dict[str, Any]] = []
        seen_osm_ids: set[int] = set()
        for el in raw.get("elements", []):
            row = _element_to_row(el)
            if row is None:
                continue
            osm_id = row["osm_id"]
            if osm_id in seen_osm_ids:
                continue
            seen_osm_ids.add(osm_id)
            rows.append(row)

        if not rows:
            log.warning("osm.sez.empty", note="No SEZ features returned from Overpass")
            run.row_count = 0
            return 0

        df = pd.DataFrame(rows)
        clean, rejected = validate_and_split(
            df.drop(columns=["centroid_wkt"]),
            contract,
            run_id=str(run.run_id),
            source="osm.sez",
        )

        # Re-attach centroid_wkt for the insert (not in the contract on
        # purpose — it's a derived geometry, not source data).
        clean = clean.merge(
            df[["osm_id", "centroid_wkt"]], on="osm_id", how="left"
        )
        clean["ingestion_run_id"] = str(run.run_id)
        clean["state_code"] = None  # populated by features layer later

        # Two-step insert: main geom via the standard writer, then update
        # centroid + state_code in place. Keeps the writer generic.
        n = _insert_with_centroid(clean.to_dict(orient="records"))
        run.row_count = n
        run.rows_rejected = rejected
        return n


def _insert_with_centroid(rows: list[dict[str, Any]]) -> int:
    from sqlalchemy import text

    from app.core.db import bulk_execute, session_scope

    if not rows:
        return 0
    sql = (
        "INSERT INTO dc_india.raw_sez "
        "(osm_id, name, operator, policy_tag, state_code, geom, centroid, ingestion_run_id) "
        "VALUES (:osm_id, :name, :operator, :policy_tag, :state_code, "
        "        ST_GeomFromText(:wkt, 4326), "
        "        ST_GeomFromText(:centroid_wkt, 4326), "
        "        :ingestion_run_id)"
    )
    n = bulk_execute(sql, rows)
    log.info("osm.sez.inserted", n=n)

    # Stamp state_code from raw india_states intersection in one shot.
    with session_scope() as session:
        session.execute(
            text(
                """
                UPDATE dc_india.raw_sez s
                SET state_code = st.state_code
                FROM dc_india.india_states st
                WHERE ST_Intersects(s.centroid, st.geom)
                  AND s.state_code IS NULL
                """
            )
        )
    return n

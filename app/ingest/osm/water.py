"""OSM water bodies + rivers ingest."""
from __future__ import annotations

from typing import Any

import pandas as pd
from shapely.geometry import LineString, Polygon

from app.core.config import load_sources
from app.core.logging import get_logger
from app.governance.contracts import get_contract, schema_hash
from app.governance.lineage import ingestion_run, should_skip
from app.ingest.base import validate_and_split
from app.ingest.osm import overpass
from app.ingest.osm._writers import insert_rows, truncate

log = get_logger("ingest.osm.water")


def _element_to_wkt(el: dict[str, Any]) -> tuple[str, str] | None:
    """Return (wkt, kind) for the OSM element or None if unrenderable."""
    tags = el.get("tags", {})
    if el.get("type") == "way" and "geometry" in el:
        coords = [(pt["lon"], pt["lat"]) for pt in el["geometry"]]
        if len(coords) < 2:
            return None
        if tags.get("waterway") == "river":
            return LineString(coords).wkt, "river"
        # Closed way → polygon body of water.
        if coords[0] == coords[-1] and len(coords) >= 4:
            return Polygon(coords).wkt, _classify_kind(tags)
        return LineString(coords).wkt, _classify_kind(tags)
    # Relations: accept only when Overpass returned a center, treat as a small polygon.
    if el.get("type") == "relation" and "center" in el:
        lon, lat = el["center"]["lon"], el["center"]["lat"]
        # Buffer the centroid by ~250m to get a stand-in polygon.
        return Polygon([(lon - 0.0025, lat - 0.0025), (lon + 0.0025, lat - 0.0025),
                        (lon + 0.0025, lat + 0.0025), (lon - 0.0025, lat + 0.0025),
                        (lon - 0.0025, lat - 0.0025)]).wkt, _classify_kind(tags)
    return None


def _classify_kind(tags: dict[str, str]) -> str:
    w = tags.get("water") or tags.get("natural") or tags.get("waterway") or "lake"
    mapping = {
        "reservoir": "reservoir",
        "pond": "pond",
        "lake": "lake",
        "river": "river",
        "stream": "river",
    }
    return mapping.get(w, "lake")


def ingest_water(*, fresh: bool = False) -> int:
    if existing := should_skip("osm.water_bodies", fresh=fresh):
        log.info("ingest.skip_recent", source="osm.water_bodies", existing_run_id=str(existing))
        return 0

    sources = load_sources()
    q = sources["osm_overpass"]["water_bodies"]["query"]
    contract = get_contract("osm.water_bodies")

    with ingestion_run(
        source="osm.water_bodies",
        upstream_source="overpass[natural=water OR waterway=river]",
        schema_hash=schema_hash(contract),
    ) as run:
        truncate("raw_water_bodies")
        raw = overpass.fetch(q)
        elements = raw.get("elements", [])
        del raw  # Free the parsed Overpass response — can be 100+ MB at pan-India scale.
        rows = []
        for el in elements:
            parsed = _element_to_wkt(el)
            if not parsed:
                continue
            wkt_str, kind = parsed
            rows.append(
                {
                    "osm_id": el["id"],
                    "name": el.get("tags", {}).get("name"),
                    "kind": kind,
                    "wkt": wkt_str,
                }
            )
        del elements
        df = pd.DataFrame(rows)
        del rows
        clean, rejected = validate_and_split(
            df, contract, run_id=str(run.run_id), source="osm.water_bodies"
        )
        clean["ingestion_run_id"] = str(run.run_id)
        n = insert_rows("raw_water_bodies", clean.to_dict(orient="records"))
        run.row_count = n
        run.rows_rejected = rejected
        return n

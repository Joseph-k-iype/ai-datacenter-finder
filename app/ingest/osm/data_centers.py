"""OSM data center / hyperscaler footprint ingest.

Pulls features tagged as data centers (telecom/office/building/industrial
variants) plus name-matched candidates. Centroid-only — most OSM data
centers are mapped as points or small footprints, and what we care about
downstream is the location for distance queries, not the parcel shape.

Company / tier extraction is regex-based on tags + name. OSM is
inconsistent here so we treat both fields as best-effort.
"""
from __future__ import annotations

import re
from typing import Any

import pandas as pd

from app.core.config import load_sources
from app.core.logging import get_logger
from app.governance.contracts import get_contract, schema_hash
from app.governance.lineage import ingestion_run, should_skip
from app.ingest.base import validate_and_split
from app.ingest.osm import overpass
from app.ingest.osm._writers import insert_rows, overpass_nodes_to_points, truncate

log = get_logger("ingest.osm.data_centers")


# Common India-relevant operators. We treat these as canonical companies
# rather than letting OSM's freeform `operator` tag drive the graph
# stakeholder layer.
_COMPANIES: list[tuple[str, re.Pattern[str]]] = [
    ("Amazon Web Services", re.compile(r"\b(AWS|Amazon)\b", re.I)),
    ("Microsoft Azure", re.compile(r"\b(Azure|Microsoft)\b", re.I)),
    ("Google Cloud", re.compile(r"\b(GCP|Google)\b", re.I)),
    ("Oracle Cloud", re.compile(r"\bOracle\b", re.I)),
    ("IBM Cloud", re.compile(r"\bIBM\b", re.I)),
    ("Yotta Infrastructure", re.compile(r"\bYotta\b", re.I)),
    ("NTT", re.compile(r"\bNTT\b", re.I)),
    ("CtrlS Datacenters", re.compile(r"\bCtrl[- ]?S\b", re.I)),
    ("NXTRA by Airtel", re.compile(r"\b(NXTRA|Airtel)\b", re.I)),
    ("Reliance Jio", re.compile(r"\b(Jio|Reliance)\b", re.I)),
    ("Tata Communications", re.compile(r"\bTata\b", re.I)),
]

_TIER_RE = re.compile(r"\btier[- ]?(?:I{1,4}|[1-4])\b", re.I)


def _classify_company(name: str | None, operator: str | None) -> str | None:
    text = " ".join(filter(None, [name, operator]))
    if not text.strip():
        return None
    for canonical, pattern in _COMPANIES:
        if pattern.search(text):
            return canonical
    return operator or None


def _classify_tier(name: str | None, tags: dict[str, str]) -> int | None:
    raw = tags.get("tier") or (name or "")
    m = _TIER_RE.search(raw)
    if not m:
        return None
    word = m.group(0).upper().replace(" ", "").replace("-", "")
    # Strip prefix "TIER".
    suffix = word[4:]
    mapping = {"I": 1, "II": 2, "III": 3, "IV": 4, "1": 1, "2": 2, "3": 3, "4": 4}
    return mapping.get(suffix)


def ingest_data_centers(*, fresh: bool = False) -> int:
    if existing := should_skip("osm.data_centers", fresh=fresh):
        log.info(
            "ingest.skip_recent",
            source="osm.data_centers",
            existing_run_id=str(existing),
        )
        return 0

    sources = load_sources()
    q = sources["osm_overpass"]["data_centers"]["query"]
    contract = get_contract("osm.data_centers")

    with ingestion_run(
        source="osm.data_centers",
        upstream_source="overpass[telecom=data_center | building=data_center | name~data center]",
        schema_hash=schema_hash(contract),
    ) as run:
        truncate("raw_data_centers")
        raw = overpass.fetch(q)
        points = overpass_nodes_to_points(raw.get("elements", []))

        rows: list[dict[str, Any]] = []
        seen: set[int] = set()
        for p in points:
            osm_id = p["osm_id"]
            if osm_id in seen:
                continue
            seen.add(osm_id)
            tags = p["tags"]
            name = tags.get("name")
            company = _classify_company(name, tags.get("operator"))
            tier = _classify_tier(name, tags)
            rows.append(
                {
                    "osm_id": osm_id,
                    "name": name,
                    "operator": tags.get("operator"),
                    "company": company,
                    "tier": tier,
                    "city": tags.get("addr:city") or tags.get("city"),
                    "wkt": p["wkt"],
                }
            )

        if not rows:
            log.warning(
                "osm.data_centers.empty",
                note="No data-center features returned from Overpass",
            )
            run.row_count = 0
            return 0

        df = pd.DataFrame(rows)
        clean, rejected = validate_and_split(
            df, contract, run_id=str(run.run_id), source="osm.data_centers"
        )
        clean["ingestion_run_id"] = str(run.run_id)
        clean["state_code"] = None
        n = insert_rows(
            "raw_data_centers",
            clean[
                [
                    "osm_id",
                    "name",
                    "operator",
                    "company",
                    "tier",
                    "city",
                    "state_code",
                    "wkt",
                    "ingestion_run_id",
                ]
            ].to_dict(orient="records"),
        )

        # Backfill state_code from india_states polygons.
        _stamp_state_codes()

        run.row_count = n
        run.rows_rejected = rejected
        return n


def _stamp_state_codes() -> None:
    from sqlalchemy import text

    from app.core.db import session_scope

    with session_scope() as session:
        session.execute(
            text(
                """
                UPDATE dc_india.raw_data_centers dc
                SET state_code = st.state_code
                FROM dc_india.india_states st
                WHERE ST_Intersects(dc.geom, st.geom)
                  AND dc.state_code IS NULL
                """
            )
        )

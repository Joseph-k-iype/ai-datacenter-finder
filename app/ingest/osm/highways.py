"""OSM motorway + trunk ingest (fiber-RoW proxy)."""
from __future__ import annotations

import pandas as pd

from app.core.config import load_sources
from app.governance.contracts import get_contract, schema_hash
from app.governance.lineage import ingestion_run
from app.ingest.base import validate_and_split
from app.ingest.osm import overpass
from app.ingest.osm._writers import insert_rows, overpass_ways_to_linestrings, truncate


def ingest_highways() -> int:
    sources = load_sources()
    q = sources["osm_overpass"]["highways"]["query"]
    contract = get_contract("osm.highways")

    with ingestion_run(
        source="osm.highways",
        upstream_source="overpass[highway=motorway|trunk]",
        schema_hash=schema_hash(contract),
    ) as run:
        truncate("raw_highways")
        raw = overpass.fetch(q)
        rows = []
        for r in overpass_ways_to_linestrings(raw.get("elements", [])):
            tags = r["tags"]
            cls = tags.get("highway") or "trunk"
            if cls not in {"motorway", "trunk"}:
                continue
            rows.append(
                {
                    "osm_id": r["osm_id"],
                    "ref": tags.get("ref"),
                    "classification": cls,
                    "wkt": r["wkt"],
                }
            )
        df = pd.DataFrame(rows)
        clean, rejected = validate_and_split(
            df, contract, run_id=str(run.run_id), source="osm.highways"
        )
        clean["ingestion_run_id"] = str(run.run_id)
        n = insert_rows("raw_highways", clean.to_dict(orient="records"))
        run.row_count = n
        run.rows_rejected = rejected
        return n

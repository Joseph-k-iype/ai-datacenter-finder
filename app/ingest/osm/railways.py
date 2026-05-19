"""OSM railway=rail ingest (secondary fiber-RoW proxy)."""
from __future__ import annotations

import pandas as pd

from app.core.config import load_sources
from app.governance.contracts import get_contract, schema_hash
from app.governance.lineage import ingestion_run
from app.ingest.base import validate_and_split
from app.ingest.osm import overpass
from app.ingest.osm._writers import insert_rows, overpass_ways_to_linestrings, truncate


def ingest_railways() -> int:
    sources = load_sources()
    q = sources["osm_overpass"]["railways"]["query"]
    contract = get_contract("osm.railways")

    with ingestion_run(
        source="osm.railways",
        upstream_source="overpass[railway=rail]",
        schema_hash=schema_hash(contract),
    ) as run:
        truncate("raw_railways")
        raw = overpass.fetch(q)
        rows = []
        for r in overpass_ways_to_linestrings(raw.get("elements", [])):
            rows.append({"osm_id": r["osm_id"], "wkt": r["wkt"]})
        df = pd.DataFrame(rows)
        clean, rejected = validate_and_split(
            df, contract, run_id=str(run.run_id), source="osm.railways"
        )
        clean["ingestion_run_id"] = str(run.run_id)
        n = insert_rows("raw_railways", clean.to_dict(orient="records"))
        run.row_count = n
        run.rows_rejected = rejected
        return n

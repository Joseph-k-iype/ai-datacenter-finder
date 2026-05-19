"""Curated major-metros (population anchors)."""
from __future__ import annotations

import pandas as pd
from shapely.geometry import Point

from app.core.config import load_sources
from app.governance.contracts import get_contract, schema_hash
from app.governance.lineage import ingestion_run
from app.ingest.base import validate_and_split
from app.ingest.osm._writers import insert_rows, truncate


def ingest() -> int:
    sources = load_sources()
    raw = sources["metros_in"]
    contract = get_contract("static.metros")

    with ingestion_run(
        source="static.metros",
        upstream_source="configs/sources.yml#metros_in",
        schema_hash=schema_hash(contract),
    ) as run:
        truncate("raw_metros")
        df = pd.DataFrame(
            [
                {
                    "name": r["name"],
                    "population": r["population"],
                    "state_code": r.get("state_code"),
                    "lon": r["coords"][0],
                    "lat": r["coords"][1],
                }
                for r in raw
            ]
        )
        clean, rejected = validate_and_split(
            df, contract, run_id=str(run.run_id), source="static.metros"
        )
        clean["wkt"] = clean.apply(lambda r: Point(r.lon, r.lat).wkt, axis=1)
        clean["ingestion_run_id"] = str(run.run_id)
        rows = clean[["name", "population", "state_code", "wkt", "ingestion_run_id"]].to_dict(
            orient="records"
        )
        n = insert_rows("raw_metros", rows)
        run.row_count = n
        run.rows_rejected = rejected
        return n

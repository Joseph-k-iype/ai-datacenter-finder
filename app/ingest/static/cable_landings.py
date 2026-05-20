"""Curated submarine-cable landing stations from sources.yml."""
from __future__ import annotations

import pandas as pd
from shapely.geometry import Point

from app.core.config import load_sources
from app.core.logging import get_logger
from app.governance.contracts import get_contract, schema_hash
from app.governance.lineage import ingestion_run, should_skip
from app.ingest.base import validate_and_split
from app.ingest.osm._writers import insert_rows, truncate

log = get_logger("ingest.static.cable_landings")


def ingest(*, fresh: bool = False) -> int:
    if existing := should_skip("static.cable_landings", fresh=fresh):
        log.info(
            "ingest.skip_recent",
            source="static.cable_landings",
            existing_run_id=str(existing),
        )
        return 0

    sources = load_sources()
    raw = sources["cable_landings_in"]
    contract = get_contract("static.cable_landings")

    with ingestion_run(
        source="static.cable_landings",
        upstream_source="configs/sources.yml#cable_landings_in",
        schema_hash=schema_hash(contract),
    ) as run:
        truncate("raw_cable_landings")
        df = pd.DataFrame(
            [
                {
                    "name": r["name"],
                    "city": r.get("city"),
                    "lon": r["coords"][0],
                    "lat": r["coords"][1],
                    "operators": r.get("operators", []),
                    "cables": r.get("cables", []),
                }
                for r in raw
            ]
        )
        clean, rejected = validate_and_split(
            df[["name", "city", "lon", "lat"]],
            contract,
            run_id=str(run.run_id),
            source="static.cable_landings",
        )
        merged = clean.merge(
            df[["name", "operators", "cables"]], on="name", how="left"
        )
        merged["wkt"] = merged.apply(lambda r: Point(r.lon, r.lat).wkt, axis=1)
        merged["ingestion_run_id"] = str(run.run_id)
        # psycopg3 maps Python lists to PostgreSQL ARRAY natively, so no
        # explicit cast is required as long as we pass list[str] (not str).
        merged["operators"] = merged["operators"].apply(lambda v: list(v) if v else [])
        merged["cables"] = merged["cables"].apply(lambda v: list(v) if v else [])
        rows = merged[["name", "city", "operators", "cables", "wkt", "ingestion_run_id"]].to_dict(
            orient="records"
        )
        n = insert_rows("raw_cable_landings", rows)
        run.row_count = n
        run.rows_rejected = rejected
        return n

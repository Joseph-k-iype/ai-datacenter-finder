"""JRC Global Surface Water occurrence ingest (% of months water present, 1984+)."""
from __future__ import annotations

import ee

from app.core.config import load_pipeline_config
from app.governance.contracts import get_contract, schema_hash
from app.governance.lineage import ingestion_run
from app.ingest.base import validate_and_split
from app.ingest.gee._common import upsert_zonal
from app.ingest.gee.zonal_export import run_zonal_export


def ingest(resolution: int = 7) -> int:
    cfg = load_pipeline_config()
    asset = cfg["gee"]["layers"]["flood"]
    scale = cfg["gee"]["scale_m"]["flood"]
    contract = get_contract("gee.flood")

    with ingestion_run(
        source="gee.flood",
        upstream_source=asset,
        schema_hash=schema_hash(contract),
    ) as run:
        gsw = ee.Image(asset).select(["occurrence", "seasonality"])
        df = run_zonal_export(
            image=gsw,
            reducer=ee.Reducer.mean(),
            band_names=["occurrence", "seasonality"],
            resolution=resolution,
            export_name="flood_gsw",
            scale_m=scale,
        )
        df = df.rename(
            columns={"occurrence": "occurrence_pct", "seasonality": "seasonality"}
        )[["h3_id", "occurrence_pct", "seasonality"]]
        df["occurrence_pct"] = df["occurrence_pct"].fillna(0.0).clip(0.0, 100.0)

        clean, rejected = validate_and_split(
            df, contract, run_id=str(run.run_id), source="gee.flood"
        )
        n = upsert_zonal(
            df=clean,
            table="raster_zonal_flood",
            resolution=resolution,
            run_id=str(run.run_id),
            columns=["occurrence_pct", "seasonality"],
        )
        run.row_count = n
        run.rows_rejected = rejected
        return n

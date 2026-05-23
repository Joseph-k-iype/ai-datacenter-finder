"""JRC Global Surface Water occurrence ingest (% of months water present, 1984+)."""
from __future__ import annotations

import ee

from app.core.config import load_pipeline_config
from app.core.logging import get_logger
from app.governance.contracts import get_contract, schema_hash
from app.governance.lineage import ingestion_run, should_skip
from app.ingest.base import validate_and_split
from app.ingest.gee._common import upsert_zonal
from app.ingest.gee.zonal_export import run_zonal_export

log = get_logger("ingest.gee.flood")


def ingest(resolution: int = 7, *, fresh: bool = False) -> int:
    if existing := should_skip("gee.flood", fresh=fresh):
        log.info("ingest.skip_recent", source="gee.flood", existing_run_id=str(existing))
        return 0

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

        stats = {"upserted": 0, "rejected": 0}

        def _consume(df) -> None:
            df = df.rename(
                columns={"occurrence": "occurrence_pct", "seasonality": "seasonality"}
            )[["h3_id", "occurrence_pct", "seasonality"]]
            df["occurrence_pct"] = df["occurrence_pct"].fillna(0.0).clip(0.0, 100.0)
            clean, rejected = validate_and_split(
                df, contract, run_id=str(run.run_id), source="gee.flood"
            )
            stats["upserted"] += upsert_zonal(
                df=clean,
                table="raster_zonal_flood",
                resolution=resolution,
                run_id=str(run.run_id),
                columns=["occurrence_pct", "seasonality"],
            )
            stats["rejected"] += rejected

        run_zonal_export(
            image=gsw,
            reducer=ee.Reducer.mean(),
            band_names=["occurrence", "seasonality"],
            resolution=resolution,
            export_name="flood_gsw",
            scale_m=scale,
            on_chunk=_consume,
        )

        run.row_count = stats["upserted"]
        run.rows_rejected = stats["rejected"]
        return stats["upserted"]

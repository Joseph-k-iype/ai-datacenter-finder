"""SRTM 30m slope ingest (mean / max / p95 in degrees)."""
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
    asset = cfg["gee"]["layers"]["slope"]
    scale = cfg["gee"]["scale_m"]["slope"]
    contract = get_contract("gee.slope")

    with ingestion_run(
        source="gee.slope",
        upstream_source=asset,
        schema_hash=schema_hash(contract),
    ) as run:
        srtm = ee.Image(asset)
        slope = ee.Terrain.slope(srtm).rename("slope_deg")
        reducer = (
            ee.Reducer.mean()
            .combine(ee.Reducer.max(), sharedInputs=True)
            .combine(ee.Reducer.percentile([95]), sharedInputs=True)
        )
        df = run_zonal_export(
            image=slope,
            reducer=reducer,
            band_names=["slope_deg"],
            resolution=resolution,
            export_name="slope_srtm",
            scale_m=scale,
        )
        df = df.rename(
            columns={
                "slope_deg_mean": "mean_slope_deg",
                "slope_deg_max": "max_slope_deg",
                "slope_deg_p95": "p95_slope_deg",
            }
        )[["h3_id", "mean_slope_deg", "max_slope_deg", "p95_slope_deg"]]

        clean, rejected = validate_and_split(
            df, contract, run_id=str(run.run_id), source="gee.slope"
        )
        n = upsert_zonal(
            df=clean,
            table="raster_zonal_slope",
            resolution=resolution,
            run_id=str(run.run_id),
            columns=["mean_slope_deg", "max_slope_deg", "p95_slope_deg"],
        )
        run.row_count = n
        run.rows_rejected = rejected
        return n

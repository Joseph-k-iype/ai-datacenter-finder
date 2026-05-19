"""Seismic hazard (PGA) zonal ingest.

Uses NASA SEDAC GSHAP global PGA raster (uploaded as a GEE asset per
configs/pipeline.yml). Marks cells in Zone V (PGA > 0.36g) for exclusion.
"""
from __future__ import annotations

import ee

from app.core.config import load_exclusions, load_pipeline_config
from app.governance.contracts import get_contract, schema_hash
from app.governance.lineage import ingestion_run
from app.ingest.base import validate_and_split
from app.ingest.gee._common import upsert_zonal
from app.ingest.gee.zonal_export import run_zonal_export


def ingest(resolution: int = 7) -> int:
    cfg = load_pipeline_config()
    exclusions = load_exclusions()
    asset = cfg["gee"]["layers"]["seismic"]
    scale = cfg["gee"]["scale_m"]["seismic"]
    threshold = float(exclusions["seismic"]["exclude_pga_g_gt"])

    contract = get_contract("gee.seismic")

    with ingestion_run(
        source="gee.seismic",
        upstream_source=asset,
        schema_hash=schema_hash(contract),
    ) as run:
        image = ee.Image(asset)
        df = run_zonal_export(
            image=image,
            reducer=ee.Reducer.mean(),
            band_names=image.bandNames().getInfo(),
            resolution=resolution,
            export_name="seismic_pga",
            scale_m=scale,
        )
        # Normalize to a single 'pga_g' column.
        value_col = next(c for c in df.columns if c != "h3_id")
        df = df.rename(columns={value_col: "pga_g"})[["h3_id", "pga_g"]]
        df["in_zone_v"] = df["pga_g"] > threshold

        clean, rejected = validate_and_split(
            df.drop(columns=["in_zone_v"]),
            contract,
            run_id=str(run.run_id),
            source="gee.seismic",
        )
        clean["in_zone_v"] = (clean["pga_g"] > threshold).astype(bool)

        n = upsert_zonal(
            df=clean,
            table="raster_zonal_seismic",
            resolution=resolution,
            run_id=str(run.run_id),
            columns=["pga_g", "in_zone_v"],
        )
        run.row_count = n
        run.rows_rejected = rejected
        return n

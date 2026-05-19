"""Solar yield ingest. Uses an uploaded Global Solar Atlas PVOUT asset.

If GSA isn't available, falls back to deriving annual GHI from NASA POWER /
SARAH-2 raster collections. Output: annual PVOUT (kWh/kWp/yr) + GHI (kWh/m²).
"""
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
    asset = cfg["gee"]["layers"]["solar"]
    scale = cfg["gee"]["scale_m"]["solar"]
    contract = get_contract("gee.solar")

    with ingestion_run(
        source="gee.solar",
        upstream_source=asset,
        schema_hash=schema_hash(contract),
    ) as run:
        try:
            pvout_img = ee.Image(asset).select(["pvout_specific"]).rename("pvout_kwh_per_kwp")
            ghi_img = ee.Image(asset).select(["ghi"]).rename("ghi_kwh_per_m2")
            combined = pvout_img.addBands(ghi_img)
            band_names = ["pvout_kwh_per_kwp", "ghi_kwh_per_m2"]
        except Exception:
            # Fallback: use ERA5-Land surface_solar_radiation_downwards (annual sum).
            era5 = (
                ee.ImageCollection("ECMWF/ERA5_LAND/MONTHLY")
                .filterDate("2023-01-01", "2024-01-01")
                .select("surface_solar_radiation_downwards_sum")
                .sum()
                .divide(3.6e6)  # J/m² → kWh/m²
                .rename("ghi_kwh_per_m2")
            )
            # Rule-of-thumb: PVOUT ≈ 0.75 × GHI for c-Si fixed-tilt in India climate.
            pvout = era5.multiply(0.75).rename("pvout_kwh_per_kwp")
            combined = ee.Image.cat([pvout, era5])
            band_names = ["pvout_kwh_per_kwp", "ghi_kwh_per_m2"]

        df = run_zonal_export(
            image=combined,
            reducer=ee.Reducer.mean(),
            band_names=band_names,
            resolution=resolution,
            export_name="solar_pvout",
            scale_m=scale,
        )
        df = df[["h3_id", "pvout_kwh_per_kwp", "ghi_kwh_per_m2"]]

        clean, rejected = validate_and_split(
            df, contract, run_id=str(run.run_id), source="gee.solar"
        )
        n = upsert_zonal(
            df=clean,
            table="raster_zonal_solar",
            resolution=resolution,
            run_id=str(run.run_id),
            columns=["pvout_kwh_per_kwp", "ghi_kwh_per_m2"],
        )
        run.row_count = n
        run.rows_rejected = rejected
        return n

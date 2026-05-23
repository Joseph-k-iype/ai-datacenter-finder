"""Solar yield ingest.

Tries the user's uploaded Global Solar Atlas PVOUT asset first; if it
doesn't exist, **falls back automatically to deriving annual yield from
ERA5-Land's ``surface_solar_radiation_downwards_sum``**. This means the
layer always produces a row per cell — never crashes the pipeline just
because the user hasn't uploaded GSA.

Upload instructions (preferred — higher accuracy than ERA5):
    earthengine upload image \\
        --asset_id projects/<your-project>/assets/global_solar_atlas_pvout \\
        gs://<your-bucket>/pvout_specific_lta.tif

ERA5 fallback uses 0.75 × annual GHI as a c-Si fixed-tilt yield estimate
appropriate for the Indian climate.
"""
from __future__ import annotations

import ee

from app.core.config import get_settings, load_pipeline_config
from app.core.logging import get_logger
from app.governance.contracts import get_contract, schema_hash
from app.governance.lineage import ingestion_run, should_skip
from app.ingest.base import validate_and_split
from app.ingest.gee._assets import asset_exists
from app.ingest.gee._common import upsert_zonal
from app.ingest.gee.client import init_ee
from app.ingest.gee.zonal_export import run_zonal_export

log = get_logger("ingest.gee.solar")


def _resolve_asset_id() -> str:
    settings = get_settings()
    asset_template = load_pipeline_config()["gee"]["layers"]["solar"]
    return asset_template.format(project=settings.gee_project)


def _era5_fallback(cfg: dict) -> ee.Image:
    """Derive PVOUT + GHI from ERA5-Land monthly surface solar radiation."""
    window = cfg["gee"]["windows"]["climate"]
    era5_ghi = (
        ee.ImageCollection("ECMWF/ERA5_LAND/MONTHLY_AGGR")
        .filterDate(window["start_date"], window["end_date"])
        .select("surface_solar_radiation_downwards_sum")
        .sum()
        .divide(3.6e6)               # J/m² → kWh/m² annual
        .rename("ghi_kwh_per_m2")
    )
    # c-Si fixed-tilt rule of thumb for Indian climate.
    era5_pvout = era5_ghi.multiply(0.75).rename("pvout_kwh_per_kwp")
    return ee.Image.cat([era5_pvout, era5_ghi])


def ingest(resolution: int = 7, *, fresh: bool = False) -> int:
    if existing := should_skip("gee.solar", fresh=fresh):
        log.info("ingest.skip_recent", source="gee.solar", existing_run_id=str(existing))
        return 0

    init_ee()
    cfg = load_pipeline_config()
    asset = _resolve_asset_id()
    scale = cfg["gee"]["scale_m"]["solar"]
    contract = get_contract("gee.solar")

    with ingestion_run(
        source="gee.solar",
        upstream_source=asset,
        schema_hash=schema_hash(contract),
    ) as run:
        if asset_exists(asset):
            log.info("gee.solar.source", source="global_solar_atlas", asset=asset)
            pvout_img = ee.Image(asset).select(["pvout_specific"]).rename("pvout_kwh_per_kwp")
            ghi_img = ee.Image(asset).select(["ghi"]).rename("ghi_kwh_per_m2")
            combined = pvout_img.addBands(ghi_img)
        else:
            log.warning(
                "gee.solar.fallback_to_era5",
                missing_asset=asset,
                note="Upload Global Solar Atlas PVOUT to that path for higher accuracy.",
            )
            run.notes.append(
                f"Global Solar Atlas asset '{asset}' missing. "
                "Used ERA5-Land × 0.75 fallback."
            )
            combined = _era5_fallback(cfg)

        stats = {"upserted": 0, "rejected": 0}

        def _consume(df) -> None:
            df = df[["h3_id", "pvout_kwh_per_kwp", "ghi_kwh_per_m2"]]
            clean, rejected = validate_and_split(
                df, contract, run_id=str(run.run_id), source="gee.solar"
            )
            stats["upserted"] += upsert_zonal(
                df=clean,
                table="raster_zonal_solar",
                resolution=resolution,
                run_id=str(run.run_id),
                columns=["pvout_kwh_per_kwp", "ghi_kwh_per_m2"],
            )
            stats["rejected"] += rejected

        run_zonal_export(
            image=combined,
            reducer=ee.Reducer.mean(),
            band_names=["pvout_kwh_per_kwp", "ghi_kwh_per_m2"],
            resolution=resolution,
            export_name="solar_pvout",
            scale_m=scale,
            on_chunk=_consume,
        )

        run.row_count = stats["upserted"]
        run.rows_rejected = stats["rejected"]
        return stats["upserted"]

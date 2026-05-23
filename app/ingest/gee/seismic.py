"""Seismic hazard (PGA) zonal ingest.

Uses a user-uploaded global PGA raster (typically NASA SEDAC GSHAP or the
BIS IS-1893 zone map) referenced by ``configs/pipeline.yml::gee.layers.seismic``.

If the asset isn't found in the user's GEE project, this layer **skips
cleanly with a warning** rather than crashing the whole pipeline. Downstream
exclusion logic falls back to ``in_seismic_zone_v = FALSE`` via the LEFT JOIN
in ``app/features/exclusions.py``. The Lineage page surfaces the skip so it
remains auditable.

Upload instructions:
    earthengine upload image \\
        --asset_id projects/<your-project>/assets/nasa_gshap_pga \\
        gs://<your-bucket>/gshap_pga.tif
"""
from __future__ import annotations

import ee

from app.core.config import get_settings, load_exclusions, load_pipeline_config
from app.core.logging import get_logger
from app.governance.contracts import get_contract, schema_hash
from app.governance.lineage import ingestion_run, should_skip
from app.ingest.base import validate_and_split
from app.ingest.gee._assets import asset_exists
from app.ingest.gee._common import upsert_zonal
from app.ingest.gee.client import init_ee
from app.ingest.gee.zonal_export import run_zonal_export

log = get_logger("ingest.gee.seismic")


def _resolve_asset_id() -> str:
    """Expand the {project} template in pipeline.yml against .env."""
    settings = get_settings()
    asset_template = load_pipeline_config()["gee"]["layers"]["seismic"]
    return asset_template.format(project=settings.gee_project)


def ingest(resolution: int = 7, *, fresh: bool = False) -> int:
    if existing := should_skip("gee.seismic", fresh=fresh):
        log.info("ingest.skip_recent", source="gee.seismic", existing_run_id=str(existing))
        return 0

    init_ee()
    cfg = load_pipeline_config()
    exclusions = load_exclusions()
    asset = _resolve_asset_id()
    scale = cfg["gee"]["scale_m"]["seismic"]
    threshold = float(exclusions["seismic"]["exclude_pga_g_gt"])

    contract = get_contract("gee.seismic")

    with ingestion_run(
        source="gee.seismic",
        upstream_source=asset,
        schema_hash=schema_hash(contract),
    ) as run:
        if not asset_exists(asset):
            msg = (
                f"Seismic raster GEE asset not found: '{asset}'. "
                "Upload a GSHAP / BIS IS-1893 PGA raster to that asset path "
                "(see app/ingest/gee/seismic.py docstring), or edit "
                "configs/pipeline.yml::gee.layers.seismic to point at one. "
                "Skipping seismic ingest — downstream exclusions will treat "
                "all cells as NOT in Zone V."
            )
            log.warning("gee.seismic.skipped_missing_asset", asset=asset)
            run.notes.append(msg)
            run.row_count = 0
            return 0

        image = ee.Image(asset)

        stats = {"upserted": 0, "rejected": 0}

        def _consume(df) -> None:
            value_cols = [c for c in df.columns if c != "h3_id"]
            if not value_cols:
                # No value column → skip this chunk (likely empty reduction).
                return
            df = df.rename(columns={value_cols[0]: "pga_g"})[["h3_id", "pga_g"]]
            df["in_zone_v"] = df["pga_g"] > threshold
            clean, rejected = validate_and_split(
                df.drop(columns=["in_zone_v"]),
                contract,
                run_id=str(run.run_id),
                source="gee.seismic",
            )
            clean["in_zone_v"] = (clean["pga_g"] > threshold).astype(bool)
            stats["upserted"] += upsert_zonal(
                df=clean,
                table="raster_zonal_seismic",
                resolution=resolution,
                run_id=str(run.run_id),
                columns=["pga_g", "in_zone_v"],
            )
            stats["rejected"] += rejected

        run_zonal_export(
            image=image,
            reducer=ee.Reducer.mean(),
            band_names=[],   # empty = pass through all bands
            resolution=resolution,
            export_name="seismic_pga",
            scale_m=scale,
            on_chunk=_consume,
        )
        run.row_count = stats["upserted"]
        run.rows_rejected = stats["rejected"]
        return stats["upserted"]

"""Generic GEE zonal-stats driver.

Implements the Q2-validated pattern:
  1. Reference H3 cells by their *published GEE asset ID* (uploaded once via
     ``app/grid/gee_asset.py``).
  2. Run ``reduceRegions`` server-side on the supplied ``ee.Image``.
  3. Export results to GCS as sharded CSV via
     ``ee.batch.Export.table.toCloudStorage``.
  4. Poll the task; on completion, read the CSV shards into a DataFrame
     using ``gcsfs``/``pandas``.

This is the single driver every layer-specific ingest module composes with.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable

import ee
import pandas as pd

from app.core.config import get_settings, load_pipeline_config
from app.core.gcs import read_csv_glob
from app.core.logging import get_logger
from app.ingest.gee.client import init_ee

log = get_logger("ingest.gee.zonal_export")


def run_zonal_export(
    *,
    image: ee.Image,
    reducer: ee.Reducer,
    band_names: list[str],
    resolution: int,
    export_name: str,
    scale_m: int,
    crs: str = "EPSG:4326",
    cells_asset_id: str | None = None,
    poll_seconds: int | None = None,
    timeout_minutes: int | None = None,
    tile_scale: int | None = None,
    enrich: Callable[[ee.FeatureCollection], ee.FeatureCollection] | None = None,
) -> pd.DataFrame:
    """Run a generic reduceRegions over the published H3 asset.

    Parameters
    ----------
    image
        The ``ee.Image`` to reduce.
    reducer
        ``ee.Reducer`` to apply (mean / max / percentile / etc.). Use
        ``ee.Reducer.combine`` to compute multiple stats in one pass.
    band_names
        Bands present on ``image`` whose reductions should be kept.
    resolution
        H3 resolution; resolves the cells asset.
    export_name
        Friendly task name; also used as the GCS file prefix.
    scale_m
        Reduction scale in meters. Match the native pixel size.
    cells_asset_id
        Override; default resolves from pipeline.yml.

    Returns
    -------
    DataFrame with at least ``h3_id`` + one column per reduction.
    """
    init_ee()
    settings = get_settings()
    cfg = load_pipeline_config()
    bucket = settings.gcs_bucket
    if not bucket:
        raise RuntimeError("GCS_BUCKET not configured (.env)")

    export_cfg = cfg["gee"].get("export", {})
    poll_seconds = poll_seconds or int(export_cfg.get("poll_seconds", 30))
    timeout_minutes = timeout_minutes or int(export_cfg.get("timeout_minutes", 90))
    tile_scale = tile_scale or int(export_cfg.get("tile_scale", 4))

    asset_template = cfg["gee"]["india_h3_asset"]
    asset_id = cells_asset_id or asset_template.format(project=settings.gee_project).replace(
        "h3_cells_res7", f"h3_cells_res{resolution}"
    )
    cells_fc = ee.FeatureCollection(asset_id)
    if enrich is not None:
        cells_fc = enrich(cells_fc)

    job_uid = uuid.uuid4().hex[:8]
    file_prefix = f"{settings.gcs_prefix}/{export_name}_res{resolution}_{job_uid}"

    log.info(
        "gee.zonal.start",
        export_name=export_name,
        resolution=resolution,
        cells_asset=asset_id,
        gcs_prefix=file_prefix,
        scale_m=scale_m,
    )

    reduced = image.select(band_names).reduceRegions(
        collection=cells_fc,
        reducer=reducer,
        scale=scale_m,
        crs=crs,
        tileScale=tile_scale,
    )

    # Keep only the h3_id passthrough + reducer outputs.
    select_props = ["h3_id"] + [c for c in reduced.first().propertyNames().getInfo() if c not in ("h3_id", "system:index")]
    reduced = reduced.select(propertySelectors=select_props, retainGeometry=False)

    task = ee.batch.Export.table.toCloudStorage(
        collection=reduced,
        description=f"{export_name}_res{resolution}",
        bucket=bucket,
        fileNamePrefix=file_prefix,
        fileFormat="CSV",
    )
    task.start()
    log.info("gee.zonal.task_started", task_id=task.id)

    started = time.monotonic()
    while True:
        status = task.status()
        state = status.get("state", "UNKNOWN")
        log.debug("gee.zonal.poll", state=state, task_id=task.id)
        if state in {"COMPLETED"}:
            break
        if state in {"FAILED", "CANCELLED", "CANCEL_REQUESTED"}:
            raise RuntimeError(f"GEE task {task.id} {state}: {status.get('error_message')}")
        if time.monotonic() - started > timeout_minutes * 60:
            raise TimeoutError(f"GEE task {task.id} did not finish in {timeout_minutes} min")
        time.sleep(poll_seconds)

    log.info("gee.zonal.complete", task_id=task.id, prefix=file_prefix)
    df = read_csv_glob(file_prefix)
    log.info("gee.zonal.loaded", rows=len(df), columns=list(df.columns))
    return df

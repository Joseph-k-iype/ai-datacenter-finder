"""Optional: publish H3 cells to GEE as a FeatureCollection asset.

**No longer required by the default ingestion pipeline.** The new
chunked-sync driver in ``app/ingest/gee/zonal_export.py`` streams cells
directly from Postgres in batches of ~2000 features per request — no
asset publication, no GCS bucket needed.

This module is preserved for two use cases:
  1. Users who want to use the published-asset pattern for their own
     custom GEE workflows.
  2. Future workloads that exceed the 10 MB inline payload limit even
     after chunking (extremely heavy reducers).

Calling ``push_cells_to_gee`` is safe; it remains idempotent because the
sub-asset task names are deterministic per chunk.
"""
from __future__ import annotations

import ee
from sqlalchemy import text
from tqdm import tqdm

from app.core.config import get_settings, load_pipeline_config
from app.core.db import session_scope
from app.core.logging import get_logger
from app.ingest.gee.client import init_ee

log = get_logger("grid.gee_asset")


def _fetch_cells_for_asset(resolution: int) -> list[tuple[str, float, float, str]]:
    table = f"h3_cells_res{resolution}"
    sql = text(
        f"""
        SELECT h3_id::text AS h3_id,
               ST_X(ST_Centroid(geom)) AS lon,
               ST_Y(ST_Centroid(geom)) AS lat,
               ST_AsText(geom) AS wkt
        FROM dc_india.{table}
        """
    )
    with session_scope() as session:
        return [(r[0], float(r[1]), float(r[2]), r[3]) for r in session.execute(sql).all()]


def _polygon_wkt_to_ee(wkt: str) -> ee.Geometry:
    inner = wkt[wkt.index("((") + 2 : wkt.rindex("))")]
    coords = [tuple(map(float, pair.strip().split())) for pair in inner.split(",")]
    return ee.Geometry.Polygon([list(coords)], proj=None, geodesic=False)


def push_cells_to_gee(
    resolution: int = 7,
    chunk_size: int = 5000,
    *,
    wait: bool = True,
    poll_seconds: int = 30,
    timeout_minutes: int = 60,
) -> str:
    """Push res-{resolution} cells to GEE as ``_part_NNNN`` sub-assets.

    See module docstring — most users do not need to call this. The
    default ingestion pipeline runs without any published asset.
    """
    import time

    from ee import ee_exception

    init_ee()
    settings = get_settings()
    if not settings.gee_project:
        raise RuntimeError("GEE_PROJECT not configured (.env)")

    cells = _fetch_cells_for_asset(resolution)
    if not cells:
        raise RuntimeError(f"No cells in h3_cells_res{resolution}; run `dc grid build` first.")

    cfg = load_pipeline_config()
    asset_template = cfg["gee"]["india_h3_asset"]
    asset_id = asset_template.format(project=settings.gee_project).replace(
        "h3_cells_res7", f"h3_cells_res{resolution}"
    )

    log.info(
        "gee.asset.start",
        asset_id=asset_id,
        n_cells=len(cells),
        chunk_size=chunk_size,
        note="Optional: default ingestion does not require this asset.",
    )

    tasks: list[tuple[str, ee.batch.Task]] = []
    for i in tqdm(range(0, len(cells), chunk_size), desc="upload chunks"):
        chunk = cells[i : i + chunk_size]
        feats = [
            ee.Feature(_polygon_wkt_to_ee(wkt), {"h3_id": h3_id, "lon": lon, "lat": lat})
            for h3_id, lon, lat, wkt in chunk
        ]
        fc = ee.FeatureCollection(feats)
        sub_asset = f"{asset_id}_part_{i // chunk_size:04d}"
        task = ee.batch.Export.table.toAsset(
            collection=fc,
            description=f"h3_res{resolution}_part_{i // chunk_size:04d}",
            assetId=sub_asset,
        )
        task.start()
        tasks.append((sub_asset, task))

    log.info("gee.asset.tasks_started", count=len(tasks))

    if not wait:
        log.info(
            "gee.asset.next_step",
            msg=(
                "Tasks queued asynchronously. discover_cells_collection() in "
                "app/ingest/gee/_assets.py auto-merges _part_* sub-assets when "
                "called; no manual merge step required. Monitor at: "
                "https://code.earthengine.google.com/tasks"
            ),
        )
        return asset_id

    started = time.monotonic()
    pending = list(tasks)
    while pending:
        if time.monotonic() - started > timeout_minutes * 60:
            raise TimeoutError(
                f"{len(pending)} GEE asset-upload task(s) still running after "
                f"{timeout_minutes} min. Check the EE task manager."
            )
        still_pending: list = []
        for sub_asset, task in pending:
            try:
                status = task.status()
            except ee_exception.EEException as exc:
                log.warning("gee.asset.poll_error", sub_asset=sub_asset, error=str(exc))
                still_pending.append((sub_asset, task))
                continue
            state = status.get("state", "UNKNOWN")
            if state == "COMPLETED":
                log.info("gee.asset.part_complete", sub_asset=sub_asset)
                continue
            if state in {"FAILED", "CANCELLED", "CANCEL_REQUESTED"}:
                log.error(
                    "gee.asset.part_failed",
                    sub_asset=sub_asset,
                    error=status.get("error_message"),
                )
                continue
            still_pending.append((sub_asset, task))
        pending = still_pending
        if pending:
            time.sleep(poll_seconds)

    log.info("gee.asset.all_done", parts=len(tasks), canonical_asset=asset_id)
    return asset_id

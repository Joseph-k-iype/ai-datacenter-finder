"""One-time upload of H3 cells to Google Earth Engine as a FeatureCollection asset.

This is the critical Q2 optimization: instead of sending the H3 FeatureCollection
inline on every ``reduceRegions`` call (which trips request-size limits), we
publish it once as a GEE asset and then refer to it by ID for every subsequent
zonal-stat extraction.

Pattern:
    1. Read all res-7 cell IDs + centroid + boundary from Postgres.
    2. Convert each chunk to an ``ee.FeatureCollection`` of cell-id-tagged polygons.
    3. ``ee.batch.Export.table.toAsset`` per chunk.
    4. ``ee.data.copyAsset`` or programmatic merge to a stable asset ID.

For PoC scale (~640k cells), we upload as one merged collection.
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
    """Return list of (h3_id_str, centroid_lon, centroid_lat, boundary_wkt)."""
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
    """Parse a WKT polygon and return an ee.Geometry.Polygon."""
    # Lightweight parser: 'POLYGON((x1 y1, x2 y2, ...))' — sufficient for hex boundaries.
    inner = wkt[wkt.index("((") + 2 : wkt.rindex("))")]
    coords = [tuple(map(float, pair.strip().split())) for pair in inner.split(",")]
    return ee.Geometry.Polygon([list(coords)])


def push_cells_to_gee(resolution: int = 7, chunk_size: int = 5000) -> str:
    """Push res-{resolution} cells to GEE as a FeatureCollection asset.

    Returns the asset ID.
    """
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

    log.info("gee.asset.start", asset_id=asset_id, n_cells=len(cells), chunk_size=chunk_size)

    # Build chunked FeatureCollections, export each to a sub-asset, then merge.
    # GEE's task queue is async — we kick off batches and poll.
    tasks = []
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
    log.info(
        "gee.asset.next_step",
        msg=(
            "Tasks are queued asynchronously. After all complete, run:\n"
            "  earthengine asset move/merge to consolidate parts into the canonical asset.\n"
            f"  Sub-assets: {asset_id}_part_NNNN"
        ),
    )
    return asset_id

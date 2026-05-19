"""Land-cover % already comes from GEE zonal stats and lands in
raster_zonal_landcover. This module just merges those values into
cell_features for the post-exclusion view."""
from __future__ import annotations

from sqlalchemy import text

from app.core.db import session_scope
from app.core.logging import get_logger

log = get_logger("features.coverage")


def merge_coverage(resolution: int) -> int:
    cf = f"cell_features_res{resolution}"
    sql = text(
        f"""
        UPDATE dc_india.{cf} cf
        SET urban_cover_pct  = COALESCE(lc.urban_pct,  cf.urban_cover_pct),
            forest_cover_pct = COALESCE(lc.forest_pct, cf.forest_cover_pct)
        FROM dc_india.raster_zonal_landcover lc
        WHERE lc.h3_id = cf.h3_id
          AND lc.resolution = :res
        """
    )
    with session_scope() as session:
        result = session.execute(sql, {"res": resolution})
        n = result.rowcount or 0
    log.info("features.coverage.merged", res=resolution, rows=n)
    return n

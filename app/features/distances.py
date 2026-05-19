"""Nearest-neighbor distances for cells against all infrastructure tables.

Uses PostGIS KNN (``<->`` on GiST) — extremely fast for "nearest line/point
to my centroid" semantics. One UPDATE per feature column.
"""
from __future__ import annotations

from sqlalchemy import text

from app.core.config import load_pipeline_config
from app.core.db import session_scope
from app.core.logging import get_logger

log = get_logger("features.distances")


def _default_cap_km() -> float:
    return float(load_pipeline_config()["scoring"].get("distance_cap_km", 500.0))


def _knn_update(
    feature_col: str,
    target_table: str,
    *,
    resolution: int,
    cap_km: float | None = None,
) -> int:
    if cap_km is None:
        cap_km = _default_cap_km()
    cf_table = f"cell_features_res{resolution}"
    grid_table = f"h3_cells_res{resolution}"
    sql = text(
        f"""
        WITH nearest AS (
            SELECT g.h3_id,
                   t.geom AS tgt_geom,
                   ST_DistanceSphere(ST_Centroid(g.geom), t.geom) / 1000.0 AS dist_km
            FROM dc_india.{grid_table} g
            CROSS JOIN LATERAL (
                SELECT geom
                FROM dc_india.{target_table}
                ORDER BY ST_Centroid(g.geom) <-> geom
                LIMIT 1
            ) t
        )
        UPDATE dc_india.{cf_table} cf
        SET {feature_col} = LEAST(n.dist_km, :cap)
        FROM nearest n
        WHERE cf.h3_id = n.h3_id
        """
    )
    with session_scope() as session:
        result = session.execute(sql, {"cap": cap_km})
        return result.rowcount or 0


def compute_distances(resolution: int) -> int:
    """Populate every distance column for cell_features_res{R}."""
    targets = [
        ("nearest_substation_km",     "raw_substations"),
        ("nearest_water_km",          "raw_water_bodies"),
        ("nearest_river_km",          "raw_water_bodies"),  # MVP: same source, refined later
        ("nearest_highway_km",        "raw_highways"),
        ("nearest_railway_km",        "raw_railways"),
        ("nearest_metro_km",          "raw_metros"),
        ("nearest_cable_landing_km",  "raw_cable_landings"),
    ]
    total = 0
    for col, tbl in targets:
        with session_scope() as session:
            exists = session.execute(
                text(f"SELECT COUNT(*) FROM dc_india.{tbl}")
            ).scalar_one()
        if exists == 0:
            log.warning("features.distances.skip_empty", table=tbl)
            continue
        n = _knn_update(col, tbl, resolution=resolution)
        log.info("features.distance.updated", col=col, rows=n)
        total += n
    return total

"""Dual-feed redundancy distances for every cell.

Computes two metrics per cell:

  ``nearest_hv_line_km``
      Distance to the nearest HV transmission line, regardless of sub-grid.

  ``nearest_hv_line_distinct_subgrid_km``
      Distance to the nearest HV line whose ``subgrid_component`` differs
      from the nearest-overall line's component. This is the Tier-4
      dual-feed metric — a single outage in one sub-grid does not (by
      topology) propagate to the other.

The query uses PostGIS KNN (``<->`` operator on GiST index) with
``LATERAL DISTINCT ON (subgrid_component)`` so we only consider one line
per distinct sub-grid component.
"""
from __future__ import annotations

from sqlalchemy import text

from app.core.db import session_scope
from app.core.logging import get_logger

log = get_logger("features.redundancy")


def compute_redundancy(resolution: int) -> int:
    """Populate ``nearest_hv_line_*km`` on cell_features_res{R}."""
    table = f"cell_features_res{resolution}"
    grid_table = f"h3_cells_res{resolution}"

    sql = text(
        f"""
        WITH nearest_per_subgrid AS (
            -- For each cell, get the closest line PER distinct subgrid_component.
            -- DISTINCT ON keeps only the closest line in each sub-grid.
            SELECT
                g.h3_id,
                pl.subgrid_component,
                pl.geom AS line_geom,
                ST_DistanceSphere(ST_Centroid(g.geom), pl.geom) / 1000.0 AS dist_km
            FROM dc_india.{grid_table} g
            CROSS JOIN LATERAL (
                SELECT DISTINCT ON (subgrid_component)
                       subgrid_component, geom
                FROM dc_india.raw_power_lines
                ORDER BY subgrid_component,
                         ST_Centroid(g.geom) <-> geom
            ) pl
        ),
        ranked AS (
            SELECT
                h3_id,
                subgrid_component,
                dist_km,
                ROW_NUMBER() OVER (PARTITION BY h3_id ORDER BY dist_km) AS rk
            FROM nearest_per_subgrid
        ),
        pivoted AS (
            SELECT
                h3_id,
                MAX(CASE WHEN rk = 1 THEN dist_km END) AS d1_km,
                MAX(CASE WHEN rk = 2 THEN dist_km END) AS d2_km
            FROM ranked
            GROUP BY h3_id
        )
        UPDATE dc_india.{table} cf
        SET nearest_hv_line_km                  = p.d1_km,
            nearest_hv_line_distinct_subgrid_km = p.d2_km,
            computed_at                         = now()
        FROM pivoted p
        WHERE cf.h3_id = p.h3_id
        """
    )

    with session_scope() as session:
        result = session.execute(sql)
        n = result.rowcount or 0
    log.info("features.redundancy.done", res=resolution, rows=n)
    return n

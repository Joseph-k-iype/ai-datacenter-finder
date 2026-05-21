"""Dual-feed redundancy distances for every cell.

Computes two metrics per cell:

  ``nearest_hv_line_km``
      Distance to the nearest HV transmission line, regardless of sub-grid.

  ``nearest_hv_line_distinct_subgrid_km``
      Distance to the nearest HV line whose ``subgrid_component`` differs
      from the nearest-overall line's component. This is the Tier-4
      dual-feed metric — a single outage in one sub-grid does not (by
      topology) propagate to the other.

Both metrics use PostGIS KNN (``<->`` on a GiST index) inside a LATERAL
``LIMIT 1`` — the planner walks raw_power_lines in distance order and
stops as soon as it has its answer. ~milliseconds per cell.

DON'T be tempted to combine these into a single
``LATERAL ... DISTINCT ON (subgrid_component) ORDER BY subgrid_component, <->``:
the secondary sort key invalidates the KNN index and the query becomes
a 600k × 22k cross-product (~13B comparisons, hours to run).
"""
from __future__ import annotations

from sqlalchemy import text

from app.core.db import session_scope
from app.core.logging import get_logger

log = get_logger("features.redundancy")


def compute_redundancy(resolution: int) -> int:
    """Populate ``nearest_hv_line_*km`` on cell_features_res{R}.

    Two index-friendly KNN lookups per cell (see module docstring).
    """
    table = f"cell_features_res{resolution}"
    grid_table = f"h3_cells_res{resolution}"

    sql = text(
        f"""
        WITH cells AS (
            SELECT g.h3_id, ST_Centroid(g.geom) AS centroid
            FROM dc_india.{grid_table} g
        ),
        d1 AS (
            -- Nearest line overall — pure GiST KNN.
            SELECT
                c.h3_id,
                c.centroid,
                near1.subgrid_component AS sg1,
                ST_DistanceSphere(c.centroid, near1.geom) / 1000.0 AS d1_km
            FROM cells c
            CROSS JOIN LATERAL (
                SELECT subgrid_component, geom
                FROM dc_india.raw_power_lines
                ORDER BY c.centroid <-> geom
                LIMIT 1
            ) near1
        ),
        d2 AS (
            -- Nearest line in a DIFFERENT sub-grid. Same KNN scan; the
            -- WHERE predicate is applied as the planner walks the
            -- distance-sorted candidate list. IS DISTINCT FROM also
            -- handles the (rare) NULL sg1 case — a line not in any
            -- subgrid still counts as topologically distinct.
            SELECT
                d1.h3_id,
                d1.d1_km,
                ST_DistanceSphere(d1.centroid, near2.geom) / 1000.0 AS d2_km
            FROM d1
            LEFT JOIN LATERAL (
                SELECT geom
                FROM dc_india.raw_power_lines
                WHERE subgrid_component IS DISTINCT FROM d1.sg1
                ORDER BY d1.centroid <-> geom
                LIMIT 1
            ) near2 ON TRUE
        )
        UPDATE dc_india.{table} cf
        SET nearest_hv_line_km                  = d2.d1_km,
            nearest_hv_line_distinct_subgrid_km = d2.d2_km,
            computed_at                         = now()
        FROM d2
        WHERE cf.h3_id = d2.h3_id
        """
    )

    with session_scope() as session:
        result = session.execute(sql)
        n = result.rowcount or 0
    log.info("features.redundancy.done", res=resolution, rows=n)
    return n

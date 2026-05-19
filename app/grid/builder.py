"""Populate h3_cells_res{6,7,8} from the india_states MultiPolygon.

Uses h3-pg's native ``h3_polygon_to_cells()`` in SQL — far faster than
shuttling cell IDs through Python for ~5M cells. The whole pan-India build
at res 7 is a single SQL statement that runs in under a minute on a
reasonable laptop, generated from per-state polygons to keep working memory
predictable.

Res 8 cells are *not* materialized for the whole country up front. They get
created on-demand only as children of top-scoring res-7 cells (see
``populate_drilldown_cells``).
"""
from __future__ import annotations

from sqlalchemy import text

from app.core.db import session_scope
from app.core.logging import get_logger
from app.grid.india_boundary import load_into_postgres as load_states

log = get_logger("grid.builder")


def _ensure_states() -> int:
    with session_scope() as session:
        n = int(session.execute(text("SELECT COUNT(*) FROM dc_india.india_states")).scalar_one())
    if n == 0:
        return load_states()
    return n


def build_grid(resolution: int) -> int:
    """Fill h3_cells_res{resolution} from per-state polygons.

    Returns the inserted row count.
    """
    _ensure_states()

    if resolution == 8:
        log.warning(
            "grid.res8.skipped",
            note="Res 8 cells materialize on demand via populate_drilldown_cells.",
        )
        return 0

    table = f"h3_cells_res{resolution}"

    with session_scope() as session:
        session.execute(text(f"TRUNCATE dc_india.{table}"))

        # h3-pg's polygon_to_cells emits cells whose centers fall inside the polygon.
        # We attribute each cell to its state via the state_code in the SELECT.
        # ST_PointOnSurface ensures every cell carries the state of its containing polygon.
        # Build state-by-state to keep memory bounded.
        states = session.execute(
            text("SELECT state_code, ST_AsText(geom) AS wkt FROM dc_india.india_states")
        ).all()

        total = 0
        for state_code, wkt_str in states:
            if resolution == 6:
                sql = text(
                    """
                    WITH cells AS (
                        SELECT h3_polygon_to_cells(
                                 ST_GeomFromText(:wkt, 4326)::geometry,
                                 :res
                               ) AS h3_id
                    )
                    INSERT INTO dc_india.h3_cells_res6 (h3_id, state_code, area_km2, geom)
                    SELECT h3_id,
                           :state_code,
                           h3_cell_area(h3_id, 'km^2'),
                           h3_cell_to_boundary_geometry(h3_id)
                    FROM cells
                    ON CONFLICT (h3_id) DO NOTHING
                    """
                )
            else:  # resolution == 7
                sql = text(
                    """
                    WITH cells AS (
                        SELECT h3_polygon_to_cells(
                                 ST_GeomFromText(:wkt, 4326)::geometry,
                                 :res
                               ) AS h3_id
                    )
                    INSERT INTO dc_india.h3_cells_res7 (h3_id, parent_6, state_code, area_km2, geom)
                    SELECT h3_id,
                           h3_cell_to_parent(h3_id, 6),
                           :state_code,
                           h3_cell_area(h3_id, 'km^2'),
                           h3_cell_to_boundary_geometry(h3_id)
                    FROM cells
                    ON CONFLICT (h3_id) DO NOTHING
                    """
                )

            result = session.execute(
                sql, {"wkt": wkt_str, "res": resolution, "state_code": state_code}
            )
            inserted = result.rowcount or 0
            total += inserted
            log.info("grid.state", state=state_code, res=resolution, inserted=inserted)

    log.info("grid.built", res=resolution, total=total)
    return total


def populate_drilldown_cells(score_run_id: str, top_n: int = 200) -> int:
    """Materialize res-8 children of the top-N res-7 cells from a scoring run."""
    with session_scope() as session:
        result = session.execute(
            text(
                """
                WITH top AS (
                    SELECT h3_id
                    FROM dc_india.scores_res7
                    WHERE score_run_id = :run_id
                    ORDER BY score DESC
                    LIMIT :top_n
                ),
                children AS (
                    SELECT unnest(h3_cell_to_children(t.h3_id, 8)) AS h3_id
                    FROM top t
                )
                INSERT INTO dc_india.h3_cells_res8
                  (h3_id, parent_7, parent_6, state_code, area_km2, geom)
                SELECT c.h3_id,
                       h3_cell_to_parent(c.h3_id, 7),
                       h3_cell_to_parent(c.h3_id, 6),
                       p.state_code,
                       h3_cell_area(c.h3_id, 'km^2'),
                       h3_cell_to_boundary_geometry(c.h3_id)
                FROM children c
                JOIN dc_india.h3_cells_res7 p
                  ON p.h3_id = h3_cell_to_parent(c.h3_id, 7)
                ON CONFLICT (h3_id) DO NOTHING
                """
            ),
            {"run_id": score_run_id, "top_n": top_n},
        )
        n = result.rowcount or 0
    log.info("grid.res8.drilldown", inserted=n)
    return n

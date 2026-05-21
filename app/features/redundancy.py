"""Dual-feed redundancy distances for every cell.

Computes two metrics per cell:

  ``nearest_hv_line_km``
      Distance to the nearest HV transmission line, regardless of sub-grid.

  ``nearest_hv_line_distinct_subgrid_km``
      Distance to the nearest HV line whose ``subgrid_component`` differs
      from the nearest-overall line's component. This is the Tier-4
      dual-feed metric — a single outage in one sub-grid does not (by
      topology) propagate to the other.

The metrics use PostGIS KNN (``<->`` on a GiST index) inside two LATERAL
``LIMIT 1`` lookups — the planner walks raw_power_lines in distance
order and stops at the first match. Milliseconds per cell.

DON'T be tempted to combine the two into a single
``LATERAL ... DISTINCT ON (subgrid_component) ORDER BY subgrid_component, <->``:
the secondary sort key invalidates the KNN index and the query becomes
a 600k × 22k cross-product (~13B comparisons, hours to run).

## Parallelism

State-chunked. Each Indian state's cells are an independent UPDATE
against ``cell_features_res{R}`` (disjoint rows → no lock contention).
We submit one task per state via a ``ThreadPoolExecutor`` against the
existing SQLAlchemy connection pool. The pool is sized 10+20=30, so
6 concurrent workers leaves headroom for other queries.

Per-session we also set ``max_parallel_workers_per_gather`` so the
inner KNN sweep itself uses multiple Postgres backend workers — total
in-flight parallelism is ``parallel_states × pg_workers_per_query``.

Tunables live under ``features.redundancy.*`` in ``configs/pipeline.yml``.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import text

from app.core.config import load_pipeline_config
from app.core.db import session_scope
from app.core.logging import get_logger

log = get_logger("features.redundancy")


_BASE_SQL = """
    WITH cells AS (
        SELECT g.h3_id, ST_Centroid(g.geom) AS centroid
        FROM dc_india.{grid_table} g
        {state_filter}
    ),
    d1 AS (
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
    UPDATE dc_india.{cf_table} cf
    SET nearest_hv_line_km                  = d2.d1_km,
        nearest_hv_line_distinct_subgrid_km = d2.d2_km,
        computed_at                         = now()
    FROM d2
    WHERE cf.h3_id = d2.h3_id
"""


def _run_chunk(resolution: int, state: str | None, pg_workers: int) -> int:
    """Execute the redundancy UPDATE for one state slice (or the whole grid
    if ``state`` is None). Returns rows updated."""
    grid_table = f"h3_cells_res{resolution}"
    cf_table = f"cell_features_res{resolution}"

    state_filter = "WHERE g.state_code = :state" if state else ""
    params: dict[str, str] = {"state": state} if state else {}

    sql = text(
        _BASE_SQL.format(
            grid_table=grid_table,
            cf_table=cf_table,
            state_filter=state_filter,
        )
    )

    with session_scope() as session:
        # Inner-query parallelism (per worker). SET LOCAL scopes to the
        # current transaction so concurrent connections don't trample
        # each other's planner settings. Postgres SET does NOT accept
        # bind parameters — the value must be inlined as a literal;
        # int() above enforces the type.
        session.execute(
            text(f"SET LOCAL max_parallel_workers_per_gather = {int(pg_workers)}")
        )
        session.execute(text("SET LOCAL parallel_setup_cost = 100"))
        result = session.execute(sql, params)
        return result.rowcount or 0


def _list_states(resolution: int) -> list[str]:
    grid_table = f"h3_cells_res{resolution}"
    with session_scope() as session:
        rows = session.execute(
            text(
                f"""
                SELECT DISTINCT state_code
                FROM dc_india.{grid_table}
                WHERE state_code IS NOT NULL
                ORDER BY state_code
                """
            )
        ).all()
    return [r[0] for r in rows]


def compute_redundancy(resolution: int) -> int:
    """Populate ``nearest_hv_line_*km`` on cell_features_res{R}.

    Parallel by state. See module docstring + ``configs/pipeline.yml::
    features.redundancy`` for tuning.
    """
    cfg = (
        load_pipeline_config()
        .get("features", {})
        .get("redundancy", {})
    )
    parallel_states = int(cfg.get("parallel_states", 6))
    pg_workers = int(cfg.get("pg_workers_per_query", 4))

    if parallel_states <= 1:
        n = _run_chunk(resolution, state=None, pg_workers=pg_workers)
        log.info("features.redundancy.done", res=resolution, rows=n, mode="serial")
        return n

    states = _list_states(resolution)
    if not states:
        # Empty grid — nothing to do (treat as success).
        log.warning("features.redundancy.no_states", res=resolution)
        return 0

    log.info(
        "features.redundancy.parallel_start",
        res=resolution,
        states=len(states),
        workers=parallel_states,
        pg_workers_per_query=pg_workers,
    )

    total = 0
    failed: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=parallel_states) as pool:
        futures = {
            pool.submit(_run_chunk, resolution, st, pg_workers): st for st in states
        }
        for fut in as_completed(futures):
            state = futures[fut]
            try:
                n = fut.result()
                total += n
                log.info("features.redundancy.state_done", state=state, rows=n)
            except Exception as exc:  # noqa: BLE001
                failed.append((state, str(exc)))
                log.error(
                    "features.redundancy.state_failed",
                    state=state,
                    error=str(exc),
                )

    if failed:
        # Surface the first failure — keeps the error chain readable
        # but the per-state log above has the full set.
        st, msg = failed[0]
        raise RuntimeError(
            f"redundancy failed for {len(failed)} states; first: {st}: {msg}"
        )

    log.info(
        "features.redundancy.done",
        res=resolution,
        rows=total,
        mode="parallel",
        states=len(states),
    )
    return total

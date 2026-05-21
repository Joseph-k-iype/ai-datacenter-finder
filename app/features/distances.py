"""Nearest-neighbor distances for cells against all infrastructure tables.

Uses PostGIS KNN (``<->`` on a GiST index) inside a LATERAL ``LIMIT 1``
— the planner walks the target table in distance order and stops at
the first match. Milliseconds per cell.

## Parallelism

State-chunked across an ``app.core.db`` connection pool — identical
pattern to ``app/features/redundancy.py``. Each state's cells form a
disjoint UPDATE slice on ``cell_features_res{R}``, so concurrent
workers don't contend. Tunables under
``configs/pipeline.yml::features.distances``:

  * ``parallel_states``        — outer thread count
  * ``pg_workers_per_query``   — inner Postgres parallel-query workers
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import text

from app.core.config import load_pipeline_config
from app.core.db import session_scope
from app.core.logging import get_logger

log = get_logger("features.distances")


def _default_cap_km() -> float:
    return float(load_pipeline_config()["scoring"].get("distance_cap_km", 500.0))


_KNN_SQL = """
    WITH nearest AS (
        SELECT g.h3_id,
               ST_DistanceSphere(ST_Centroid(g.geom), t.geom) / 1000.0 AS dist_km
        FROM dc_india.{grid_table} g
        {state_filter}
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


def _knn_update_chunk(
    feature_col: str,
    target_table: str,
    *,
    resolution: int,
    cap_km: float,
    state: str | None,
    pg_workers: int,
) -> int:
    grid_table = f"h3_cells_res{resolution}"
    cf_table = f"cell_features_res{resolution}"
    state_filter = "WHERE g.state_code = :state" if state else ""
    sql = text(
        _KNN_SQL.format(
            grid_table=grid_table,
            cf_table=cf_table,
            target_table=target_table,
            feature_col=feature_col,
            state_filter=state_filter,
        )
    )
    params: dict[str, object] = {"cap": cap_km}
    if state is not None:
        params["state"] = state
    with session_scope() as session:
        # Postgres SET does NOT accept bind parameters; inline as literal
        # (int() enforces the type so this is safe from injection).
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
                SELECT DISTINCT state_code FROM dc_india.{grid_table}
                WHERE state_code IS NOT NULL ORDER BY state_code
                """
            )
        ).all()
    return [r[0] for r in rows]


def _knn_update(
    feature_col: str,
    target_table: str,
    *,
    resolution: int,
    cap_km: float | None = None,
) -> int:
    """Run one KNN UPDATE pass for a (feature_col, target_table) pair.

    Splits the work by state and runs ``parallel_states`` workers in
    parallel. Falls back to a single-shot UPDATE when ``parallel_states
    <= 1``.
    """
    if cap_km is None:
        cap_km = _default_cap_km()

    cfg = load_pipeline_config().get("features", {}).get("distances", {})
    parallel_states = int(cfg.get("parallel_states", 6))
    pg_workers = int(cfg.get("pg_workers_per_query", 4))

    if parallel_states <= 1:
        return _knn_update_chunk(
            feature_col,
            target_table,
            resolution=resolution,
            cap_km=cap_km,
            state=None,
            pg_workers=pg_workers,
        )

    states = _list_states(resolution)
    if not states:
        return 0

    total = 0
    failed: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=parallel_states) as pool:
        futures = {
            pool.submit(
                _knn_update_chunk,
                feature_col,
                target_table,
                resolution=resolution,
                cap_km=cap_km,
                state=st,
                pg_workers=pg_workers,
            ): st
            for st in states
        }
        for fut in as_completed(futures):
            state = futures[fut]
            try:
                total += fut.result()
            except Exception as exc:  # noqa: BLE001
                failed.append((state, str(exc)))
                log.error(
                    "features.distance.state_failed",
                    col=feature_col,
                    state=state,
                    error=str(exc),
                )
    if failed:
        st, msg = failed[0]
        raise RuntimeError(
            f"{feature_col}: failed for {len(failed)} states; first: {st}: {msg}"
        )
    return total


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

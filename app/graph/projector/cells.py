"""Cell / State / Infrastructure projector (P2) — streaming.

Reads from:
    dc_india.h3_cells_res7, h3_cells_res8
    dc_india.india_states
    dc_india.raw_protected_areas
    dc_india.cell_features_res7
    dc_india.raw_highways, raw_railways, raw_water_bodies
    dc_india.raw_cable_landings, raw_metros

Writes to FalkorDB:
    (:State)              — one per Indian state
    (:Cell)               — one per H3 cell at res 7 / 8
    (:ProtectedArea)      — WDPA areas intersecting India
    (:Highway/Railway/WaterBody/CableLanding/Metro)
                          — supporting infrastructure
    -[:IN_STATE]->        — Cell → State
    -[:CHILD_OF]->        — res-8 Cell → res-7 Cell
    -[:NEAREST_*]->       — Cell → infrastructure (km on edge)
    -[:EXCLUDED_BY]->     — Cell → ExclusionReason
    -[:INSIDE]->          — Cell → ProtectedArea (if applicable)

Memory note: every projector reads Postgres in **batches** (yield_per)
and flushes the current batch to FalkorDB before pulling the next one.
At no point does a Python list of all 600k+ res-7 cells (or 4M+ res-8
drilldown cells) live in memory. Peak working set per projector is
``batch_size`` rows.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.core.db import session_scope
from app.core.logging import get_logger
from app.graph.client import default_batch_size, query
from app.graph.schema import E, N

log = get_logger("graph.projector.cells")

BATCH = default_batch_size()


def _iter_batches(rows_iter: Iterator[dict[str, Any]], batch_size: int) -> Iterator[list[dict[str, Any]]]:
    """Group a row-iterator into lists of ``batch_size`` items."""
    batch: list[dict[str, Any]] = []
    for row in rows_iter:
        batch.append(row)
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def _project_states() -> int:
    """States are the spatial root — every cell joins on state_code."""
    with session_scope() as session:
        rows = session.execute(
            text("SELECT state_code, state_name FROM dc_india.india_states")
        ).all()
    payload = [{"state_code": r[0], "state_name": r[1]} for r in rows]
    if not payload:
        return 0
    query(
        f"UNWIND $rows AS r MERGE (s:{N.STATE} {{state_code: r.state_code}}) "
        "SET s.state_name = r.state_name",
        {"rows": payload},
    )
    return len(payload)


def _project_cells(resolution: int) -> int:
    """Project H3 cells at a given resolution + their :IN_STATE edge — streaming."""
    upsert_cell = (
        f"UNWIND $rows AS r "
        f"MERGE (c:{N.CELL} {{h3_id: r.h3_id}}) "
        f"SET c.state_code = r.state_code, c.lat = r.lat, c.lon = r.lon, "
        f"    c.resolution = r.resolution"
    )
    link_state = (
        f"UNWIND $rows AS r "
        f"MATCH (c:{N.CELL} {{h3_id: r.h3_id}}), (s:{N.STATE} {{state_code: r.state_code}}) "
        f"MERGE (c)-[:{E.IN_STATE}]->(s)"
    )

    def _row_iter() -> Iterator[dict[str, Any]]:
        with session_scope() as session:
            result = session.execute(
                text(
                    f"""
                    SELECT h3_id::text, state_code,
                           ST_Y(ST_Centroid(geom)) AS lat,
                           ST_X(ST_Centroid(geom)) AS lon
                    FROM dc_india.h3_cells_res{resolution}
                    """
                )
            ).yield_per(BATCH)
            for r in result:
                yield {
                    "h3_id": r[0],
                    "state_code": r[1],
                    "lat": float(r[2]) if r[2] is not None else None,
                    "lon": float(r[3]) if r[3] is not None else None,
                    "resolution": resolution,
                }

    total = 0
    for chunk in _iter_batches(_row_iter(), BATCH):
        query(upsert_cell, {"rows": chunk})
        linkable = [c for c in chunk if c["state_code"]]
        if linkable:
            query(link_state, {"rows": linkable})
        total += len(chunk)
    log.info("graph.projector.cells.projected", resolution=resolution, rows=total)
    return total


def _project_cell_features(resolution: int) -> dict[str, int]:
    """Materialize NEAREST_* / property denormalisations + EXCLUDED_BY edges
    by streaming cell_features."""
    set_props = (
        f"UNWIND $rows AS r "
        f"MERGE (c:{N.CELL} {{h3_id: r.h3_id}}) "
        f"SET c.nearest_hv_line_km = r.nearest_hv_line_km, "
        f"    c.nearest_hv_line_distinct_subgrid_km = r.nearest_hv_line_distinct_subgrid_km, "
        f"    c.nearest_substation_km = r.nearest_substation_km, "
        f"    c.nearest_water_km = r.nearest_water_km, "
        f"    c.nearest_highway_km = r.nearest_highway_km, "
        f"    c.nearest_railway_km = r.nearest_railway_km, "
        f"    c.nearest_metro_km = r.nearest_metro_km, "
        f"    c.nearest_cable_landing_km = r.nearest_cable_landing_km, "
        f"    c.is_excluded = r.is_excluded, "
        f"    c.annual_pvout_kwh_per_kwp = r.annual_pvout_kwh_per_kwp, "
        f"    c.mean_temp_c = r.mean_temp_c, "
        f"    c.mean_rh_pct = r.mean_rh_pct, "
        f"    c.pop_density_per_km2 = r.pop_density_per_km2"
    )
    excl_cypher = (
        f"UNWIND $rows AS r "
        f"MATCH (c:{N.CELL} {{h3_id: r.h3_id}}), (e:{N.EXCLUSION_REASON} {{name: r.reason}}) "
        f"MERGE (c)-[:{E.EXCLUDED_BY}]->(e)"
    )

    counts = {"feature_props_set": 0, "exclusion_edges": 0}

    def _chunked_rows() -> Iterator[list[dict[str, Any]]]:
        feat_batch: list[dict[str, Any]] = []
        excl_batch: list[dict[str, str]] = []
        with session_scope() as session:
            result = session.execute(
                text(
                    f"""
                    SELECT h3_id::text,
                           nearest_hv_line_km,
                           nearest_hv_line_distinct_subgrid_km,
                           nearest_substation_km,
                           nearest_water_km,
                           nearest_highway_km,
                           nearest_railway_km,
                           nearest_metro_km,
                           nearest_cable_landing_km,
                           is_excluded,
                           exclusion_reasons,
                           annual_pvout_kwh_per_kwp,
                           mean_temp_c,
                           mean_rh_pct,
                           pop_density_per_km2
                    FROM dc_india.cell_features_res{resolution}
                    """
                )
            ).yield_per(BATCH)
            for r in result:
                h3 = r[0]
                feat_batch.append({
                    "h3_id": h3,
                    "nearest_hv_line_km": r[1],
                    "nearest_hv_line_distinct_subgrid_km": r[2],
                    "nearest_substation_km": r[3],
                    "nearest_water_km": r[4],
                    "nearest_highway_km": r[5],
                    "nearest_railway_km": r[6],
                    "nearest_metro_km": r[7],
                    "nearest_cable_landing_km": r[8],
                    "is_excluded": bool(r[9]) if r[9] is not None else False,
                    "annual_pvout_kwh_per_kwp": r[11],
                    "mean_temp_c": r[12],
                    "mean_rh_pct": r[13],
                    "pop_density_per_km2": r[14],
                })
                for reason in (r[10] or []):
                    excl_batch.append({"h3_id": h3, "reason": reason})
                if len(feat_batch) >= BATCH:
                    yield ("feat", feat_batch)
                    feat_batch = []
                if len(excl_batch) >= BATCH:
                    yield ("excl", excl_batch)
                    excl_batch = []
        if feat_batch:
            yield ("feat", feat_batch)
        if excl_batch:
            yield ("excl", excl_batch)

    for kind, chunk in _chunked_rows():  # type: ignore[misc]
        if kind == "feat":
            query(set_props, {"rows": chunk})
            counts["feature_props_set"] += len(chunk)
        else:
            query(excl_cypher, {"rows": chunk})
            counts["exclusion_edges"] += len(chunk)
    return counts


def _project_simple_infra(table: str, label: str, key_col: str, key_prop: str) -> int:
    """Project a single-table infrastructure layer — streamed.

    Tables that don't exist (not yet ingested) are silently skipped.
    """
    cypher = (
        f"UNWIND $rows AS r MERGE (n:{label} {{{key_prop}: r.{key_prop}}})"
    )

    def _row_iter() -> Iterator[dict[str, Any]]:
        try:
            with session_scope() as session:
                result = session.execute(
                    text(f"SELECT {key_col} FROM dc_india.{table}")
                ).yield_per(BATCH)
                for r in result:
                    if r[0] is not None:
                        yield {key_prop: r[0]}
        except ProgrammingError:
            log.warning("graph.projector.cells.table_missing", table=table)
            return

    total = 0
    for chunk in _iter_batches(_row_iter(), BATCH):
        query(cypher, {"rows": chunk})
        total += len(chunk)
    return total


def project_cells() -> dict[str, int]:
    """Run the full Cell+infrastructure projection. Returns row counts."""
    counts: dict[str, int] = {}
    counts["states"] = _project_states()
    counts["cells_res7"] = _project_cells(7)
    # res-8 only exists for drill-down children of top-N; safe to skip
    # if the table is empty.
    counts["cells_res8"] = _project_cells(8)
    counts.update(_project_cell_features(7))

    counts["highways"] = _project_simple_infra(
        "raw_highways", N.HIGHWAY, "osm_id", "osm_id"
    )
    counts["railways"] = _project_simple_infra(
        "raw_railways", N.RAILWAY, "osm_id", "osm_id"
    )
    counts["water_bodies"] = _project_simple_infra(
        "raw_water_bodies", N.WATER_BODY, "osm_id", "osm_id"
    )
    counts["protected_areas"] = _project_simple_infra(
        "raw_protected_areas", N.PROTECTED_AREA, "wdpa_id", "wdpa_id"
    )
    counts["cable_landings"] = _project_simple_infra(
        "raw_cable_landings", N.CABLE_LANDING, "landing_id", "landing_id"
    )
    counts["metros"] = _project_simple_infra(
        "raw_metros", N.METRO, "metro_id", "metro_id"
    )
    return counts

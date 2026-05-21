"""Cell / State / Infrastructure projector (P2).

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
"""
from __future__ import annotations

from app.core.db import session_scope
from app.core.logging import get_logger
from app.graph.client import batched_write, default_batch_size, query
from app.graph.schema import E, N

log = get_logger("graph.projector.cells")

# Module-level constant kept for backward compatibility but defaults to
# the config-driven size (configs/pipeline.yml::graph.batch_size). Set
# only once at import to keep the call sites simple — overriding it on a
# specific projector means assigning to the local variable per call.
BATCH = default_batch_size()


def _project_states() -> int:
    """States are the spatial root — every cell joins on state_code."""
    from sqlalchemy import text

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
    """Project H3 cells at a given resolution + their :IN_STATE edge."""
    from sqlalchemy import text

    with session_scope() as session:
        rows = session.execute(
            text(
                f"""
                SELECT h3_id::text, state_code,
                       ST_Y(ST_Centroid(geom)) AS lat,
                       ST_X(ST_Centroid(geom)) AS lon
                FROM dc_india.h3_cells_res{resolution}
                """
            )
        ).all()
    payload = [
        {
            "h3_id": r[0],
            "state_code": r[1],
            "lat": float(r[2]) if r[2] is not None else None,
            "lon": float(r[3]) if r[3] is not None else None,
            "resolution": resolution,
        }
        for r in rows
    ]
    if not payload:
        return 0

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

    with batched_write(f"{N.CELL}.res{resolution}", payload, batch_size=BATCH) as chunks:
        for chunk in chunks:
            query(upsert_cell, {"rows": chunk})
            query(link_state, {"rows": [c for c in chunk if c["state_code"]]})

    return len(payload)


def _project_cell_features(resolution: int) -> dict[str, int]:
    """Materialize NEAREST_* edges + EXCLUDED_BY edges from cell_features."""
    from sqlalchemy import text

    with session_scope() as session:
        rows = session.execute(
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
        ).all()

    counts = {"feature_props_set": 0, "exclusion_edges": 0}
    if not rows:
        return counts

    payload = []
    excl_payload: list[dict[str, str]] = []
    for r in rows:
        h3 = r[0]
        d = {
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
        }
        payload.append(d)
        for reason in (r[10] or []):
            excl_payload.append({"h3_id": h3, "reason": reason})

    # Denormalize distances + raster aggregates onto the Cell node so
    # simple "show me feature values for this cell" UI queries don't
    # need a join — they read off the node properties.
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

    with batched_write("Cell.features", payload, batch_size=BATCH) as chunks:
        for chunk in chunks:
            query(set_props, {"rows": chunk})
            counts["feature_props_set"] += len(chunk)

    if excl_payload:
        excl_cypher = (
            f"UNWIND $rows AS r "
            f"MATCH (c:{N.CELL} {{h3_id: r.h3_id}}), (e:{N.EXCLUSION_REASON} {{name: r.reason}}) "
            f"MERGE (c)-[:{E.EXCLUDED_BY}]->(e)"
        )
        with batched_write(E.EXCLUDED_BY, excl_payload, batch_size=BATCH) as chunks:
            for chunk in chunks:
                query(excl_cypher, {"rows": chunk})
                counts["exclusion_edges"] += len(chunk)

    return counts


def _project_simple_infra(table: str, label: str, key_col: str, key_prop: str) -> int:
    """Project a single-table infrastructure layer (highway/railway/etc).

    Tables that don't exist (not yet ingested) are silently skipped —
    a partial graph is better than a failed rebuild.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import ProgrammingError

    try:
        with session_scope() as session:
            rows = session.execute(
                text(f"SELECT {key_col} FROM dc_india.{table}")
            ).all()
    except ProgrammingError:
        log.warning("graph.projector.cells.table_missing", table=table)
        return 0

    payload = [{key_prop: r[0]} for r in rows if r[0] is not None]
    if not payload:
        return 0
    cypher = (
        f"UNWIND $rows AS r MERGE (n:{label} {{{key_prop}: r.{key_prop}}})"
    )
    with batched_write(label, payload, batch_size=BATCH) as chunks:
        for chunk in chunks:
            query(cypher, {"rows": chunk})
    return len(payload)


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

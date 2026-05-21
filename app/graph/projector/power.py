"""Power topology projector (P1).

Mirrors the NetworkX work in ``app/ingest/osm/power_topology.py`` into a
queryable property graph. Same algorithm, same outputs — just exposed
as a Cypher-traversable surface so questions like "if substation X
fails, which sites lose dual-feed?" become one-hop matches.

Reads from:
    dc_india.raw_substations    (id, osm_id, voltage_kv, operator, name)
    dc_india.raw_power_lines    (id, osm_id, voltage_kv, operator,
                                  circuits, cluster_id, subgrid_component)

Writes:
    (:Substation {osm_id})
    (:Line {osm_id})
    (:SubGrid {subgrid_id})
    (:Line)-[:CONNECTS]->(:Substation)
    (:Line)-[:PARALLEL_TO]->(:Line)              — same cluster_id
    (:Line)-[:IN_SUBGRID]->(:SubGrid)
    (:Substation)-[:IN_SUBGRID]->(:SubGrid)
    (:Cell)-[:NEAREST_LINE {km}]->(:Line)        — from cell_features
    (:Cell)-[:DUAL_FEED_LINE {km}]->(:Line)

Note on edges: the CONNECTS edge is computed here using the same
snap-endpoint-to-substation logic as the NetworkX module. We don't
materialize line-by-line endpoints in Postgres, so we re-derive them
from the geometry. This keeps the FalkorDB projection self-contained
(no dependency on a Postgres "edges" table that doesn't exist).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from shapely.geometry import Point
from shapely.strtree import STRtree
from shapely.wkt import loads as wkt_loads
from sqlalchemy import text

from app.core.config import load_pipeline_config
from app.core.db import session_scope
from app.core.logging import get_logger
from app.graph.client import batched_write, default_batch_size, query
from app.graph.schema import E, N

log = get_logger("graph.projector.power")

BATCH = default_batch_size()


def _km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Same haversine as power_topology.py — duplicated rather than imported
    to avoid a circular dep through ingest.osm.power."""
    import math

    R = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _load_substations() -> list[dict[str, Any]]:
    with session_scope() as session:
        rows = session.execute(
            text(
                """
                SELECT id, osm_id, voltage_kv, operator, name, ST_AsText(geom)
                FROM dc_india.raw_substations
                """
            )
        ).all()
    return [
        {
            "db_id": int(r[0]),
            "osm_id": int(r[1]),
            "voltage_kv": int(r[2]) if r[2] is not None else None,
            "operator": r[3],
            "name": r[4],
            "geom": wkt_loads(r[5]) if r[5] else None,
        }
        for r in rows
    ]


def _load_lines() -> list[dict[str, Any]]:
    with session_scope() as session:
        rows = session.execute(
            text(
                """
                SELECT id, osm_id, voltage_kv, operator, circuits,
                       cluster_id, subgrid_component, ST_AsText(geom)
                FROM dc_india.raw_power_lines
                """
            )
        ).all()
    return [
        {
            "db_id": int(r[0]),
            "osm_id": int(r[1]),
            "voltage_kv": int(r[2]) if r[2] is not None else None,
            "operator": r[3],
            "circuits": int(r[4]) if r[4] is not None else None,
            "cluster_id": int(r[5]) if r[5] is not None else None,
            "subgrid_id": int(r[6]) if r[6] is not None else None,
            "geom": wkt_loads(r[7]) if r[7] else None,
        }
        for r in rows
    ]


def _upsert_substations(subs: list[dict[str, Any]]) -> int:
    rows = [
        {
            "osm_id": s["osm_id"],
            "voltage_kv": s["voltage_kv"],
            "operator": s["operator"],
            "name": s["name"],
            "lat": s["geom"].y if s["geom"] else None,
            "lon": s["geom"].x if s["geom"] else None,
        }
        for s in subs
    ]
    if not rows:
        return 0
    cypher = (
        f"UNWIND $rows AS r "
        f"MERGE (s:{N.SUBSTATION} {{osm_id: r.osm_id}}) "
        f"SET s.voltage_kv = r.voltage_kv, s.operator = r.operator, "
        f"    s.name = r.name, s.lat = r.lat, s.lon = r.lon"
    )
    with batched_write(N.SUBSTATION, rows, batch_size=BATCH) as chunks:
        for chunk in chunks:
            query(cypher, {"rows": chunk})
    return len(rows)


def _upsert_lines(lines: list[dict[str, Any]]) -> int:
    rows = [
        {
            "osm_id": ln["osm_id"],
            "voltage_kv": ln["voltage_kv"],
            "operator": ln["operator"],
            "circuits": ln["circuits"],
            "cluster_id": ln["cluster_id"],
            "subgrid_id": ln["subgrid_id"],
        }
        for ln in lines
    ]
    if not rows:
        return 0
    cypher = (
        f"UNWIND $rows AS r "
        f"MERGE (l:{N.LINE} {{osm_id: r.osm_id}}) "
        f"SET l.voltage_kv = r.voltage_kv, l.operator = r.operator, "
        f"    l.circuits = r.circuits, l.cluster_id = r.cluster_id, "
        f"    l.subgrid_id = r.subgrid_id"
    )
    with batched_write(N.LINE, rows, batch_size=BATCH) as chunks:
        for chunk in chunks:
            query(cypher, {"rows": chunk})
    return len(rows)


def _upsert_subgrids(lines: list[dict[str, Any]]) -> int:
    """One SubGrid node per distinct subgrid_component value.

    The :IN_SUBGRID edge from Line and Substation is added separately so
    a SubGrid with no current substations still exists as a node.
    """
    subgrid_lines: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        sid = line["subgrid_id"]
        if sid is None:
            continue
        subgrid_lines[sid].append(line)

    rows = [
        {
            "subgrid_id": sid,
            "n_lines": len(items),
            "total_kv_km": sum(
                (i["voltage_kv"] or 0) for i in items
            ),  # rough capacity proxy; geom-based km later
        }
        for sid, items in subgrid_lines.items()
    ]
    if not rows:
        return 0
    cypher = (
        f"UNWIND $rows AS r "
        f"MERGE (g:{N.SUBGRID} {{subgrid_id: r.subgrid_id}}) "
        f"SET g.n_lines = r.n_lines, g.total_kv_km = r.total_kv_km"
    )
    with batched_write(N.SUBGRID, rows, batch_size=BATCH) as chunks:
        for chunk in chunks:
            query(cypher, {"rows": chunk})
    return len(rows)


def _link_lines_to_subgrids(lines: list[dict[str, Any]]) -> int:
    """:Line-[:IN_SUBGRID]->:SubGrid"""
    rows = [
        {"osm_id": ln["osm_id"], "subgrid_id": ln["subgrid_id"]}
        for ln in lines
        if ln["subgrid_id"] is not None
    ]
    if not rows:
        return 0
    cypher = (
        f"UNWIND $rows AS r "
        f"MATCH (l:{N.LINE} {{osm_id: r.osm_id}}), "
        f"      (g:{N.SUBGRID} {{subgrid_id: r.subgrid_id}}) "
        f"MERGE (l)-[:{E.IN_SUBGRID}]->(g)"
    )
    with batched_write(E.IN_SUBGRID, rows, batch_size=BATCH) as chunks:
        for chunk in chunks:
            query(cypher, {"rows": chunk})
    return len(rows)


def _link_parallel(lines: list[dict[str, Any]]) -> int:
    """:Line-[:PARALLEL_TO]->:Line for lines sharing a cluster_id.

    Undirected logically, modeled as one edge per ordered (a<b) pair to
    avoid duplicates on re-projection.
    """
    by_cluster: dict[int, list[int]] = defaultdict(list)
    for ln in lines:
        if ln["cluster_id"] is not None:
            by_cluster[ln["cluster_id"]].append(ln["osm_id"])

    pairs: list[dict[str, int]] = []
    for _cid, osm_ids in by_cluster.items():
        if len(osm_ids) < 2:
            continue
        osm_ids.sort()
        for i in range(len(osm_ids)):
            for j in range(i + 1, len(osm_ids)):
                pairs.append({"a": osm_ids[i], "b": osm_ids[j]})

    if not pairs:
        return 0
    cypher = (
        f"UNWIND $rows AS r "
        f"MATCH (a:{N.LINE} {{osm_id: r.a}}), (b:{N.LINE} {{osm_id: r.b}}) "
        f"MERGE (a)-[:{E.PARALLEL_TO}]->(b)"
    )
    with batched_write(E.PARALLEL_TO, pairs, batch_size=BATCH) as chunks:
        for chunk in chunks:
            query(cypher, {"rows": chunk})
    return len(pairs)


def _link_connects(
    subs: list[dict[str, Any]], lines: list[dict[str, Any]]
) -> tuple[int, int]:
    """Snap line endpoints to nearby substations to build :CONNECTS edges.

    Same logic as the NetworkX module: use a Shapely STRtree with the
    configured ``substation_snap_deg`` bbox pre-filter, then a precise
    haversine check ``<= substation_snap_km``.

    Returns (connects_edges, subgrid_substation_links).
    """
    cfg = load_pipeline_config()["power_topology"]
    snap_km = float(cfg["substation_snap_km"])
    snap_deg = float(cfg["substation_snap_deg"])

    sub_geoms = [s["geom"] for s in subs if s["geom"] is not None]
    sub_osm = [s["osm_id"] for s in subs if s["geom"] is not None]
    if not sub_geoms:
        return 0, 0
    tree = STRtree(sub_geoms)

    def _snap(pt: Point) -> int | None:
        candidates = tree.query(pt.buffer(snap_deg))
        nearest_id: int | None = None
        nearest_km = float("inf")
        for idx in candidates:
            sub = sub_geoms[idx]
            km = _km_between(pt.y, pt.x, sub.y, sub.x)
            if km <= snap_km and km < nearest_km:
                nearest_km = km
                nearest_id = sub_osm[idx]
        return nearest_id

    connects_rows: list[dict[str, Any]] = []
    sub_subgrid: set[tuple[int, int]] = set()

    for line in lines:
        if line["geom"] is None:
            continue
        coords = list(line["geom"].coords)
        if len(coords) < 2:
            continue
        snapped_subs: set[int] = set()
        for pt in (Point(*coords[0]), Point(*coords[-1])):
            sid = _snap(pt)
            if sid is not None:
                snapped_subs.add(sid)
        for sid in snapped_subs:
            connects_rows.append({"line_osm_id": line["osm_id"], "sub_osm_id": sid})
            if line["subgrid_id"] is not None:
                sub_subgrid.add((sid, line["subgrid_id"]))

    n_connects = 0
    if connects_rows:
        cypher = (
            f"UNWIND $rows AS r "
            f"MATCH (l:{N.LINE} {{osm_id: r.line_osm_id}}), "
            f"      (s:{N.SUBSTATION} {{osm_id: r.sub_osm_id}}) "
            f"MERGE (l)-[:{E.CONNECTS}]->(s)"
        )
        with batched_write(E.CONNECTS, connects_rows, batch_size=BATCH) as chunks:
            for chunk in chunks:
                query(cypher, {"rows": chunk})
                n_connects += len(chunk)

    sub_subgrid_rows = [
        {"sub_osm_id": sid, "subgrid_id": gid} for sid, gid in sub_subgrid
    ]
    n_sub_subgrid = 0
    if sub_subgrid_rows:
        cypher = (
            f"UNWIND $rows AS r "
            f"MATCH (s:{N.SUBSTATION} {{osm_id: r.sub_osm_id}}), "
            f"      (g:{N.SUBGRID} {{subgrid_id: r.subgrid_id}}) "
            f"MERGE (s)-[:{E.IN_SUBGRID}]->(g)"
        )
        with batched_write("Sub->SubGrid", sub_subgrid_rows, batch_size=BATCH) as chunks:
            for chunk in chunks:
                query(cypher, {"rows": chunk})
                n_sub_subgrid += len(chunk)

    return n_connects, n_sub_subgrid


def _link_cells_to_nearest_lines(resolution: int) -> dict[str, int]:
    """Materialize Cell-[NEAREST_LINE]->Line and Cell-[DUAL_FEED_LINE]->Line.

    The actual nearest-line *identity* is not stored in Postgres — only
    the distance is on cell_features. We re-derive identity here by
    running a quick KNN per cell: read the cell centroid + radius from
    cell_features, then a PostGIS query gets the nearest line osm_id.
    """
    from sqlalchemy.exc import ProgrammingError

    with session_scope() as session:
        try:
            rows = session.execute(
                text(
                    f"""
                    SELECT c.h3_id::text,
                           nearest.osm_id AS nearest_osm,
                           cf.nearest_hv_line_km,
                           dual.osm_id AS dual_osm,
                           cf.nearest_hv_line_distinct_subgrid_km
                    FROM dc_india.cell_features_res{resolution} cf
                    JOIN dc_india.h3_cells_res{resolution} c
                      ON c.h3_id = cf.h3_id
                    LEFT JOIN LATERAL (
                        SELECT l.osm_id
                        FROM dc_india.raw_power_lines l
                        ORDER BY ST_Centroid(c.geom) <-> l.geom
                        LIMIT 1
                    ) nearest ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT l2.osm_id
                        FROM dc_india.raw_power_lines l2
                        WHERE l2.subgrid_component IS DISTINCT FROM
                              (SELECT subgrid_component FROM dc_india.raw_power_lines l3
                               WHERE l3.osm_id = nearest.osm_id)
                        ORDER BY ST_Centroid(c.geom) <-> l2.geom
                        LIMIT 1
                    ) dual ON TRUE
                    WHERE cf.is_excluded = FALSE
                    """
                )
            ).all()
        except ProgrammingError as exc:
            log.warning(
                "graph.projector.power.cell_lines.unavailable", error=str(exc)
            )
            return {"nearest_line_edges": 0, "dual_feed_edges": 0}

    nearest_rows: list[dict[str, Any]] = []
    dual_rows: list[dict[str, Any]] = []
    for r in rows:
        h3, nearest_osm, nearest_km, dual_osm, dual_km = r
        if nearest_osm is not None:
            nearest_rows.append(
                {"h3_id": h3, "osm_id": int(nearest_osm), "km": nearest_km}
            )
        if dual_osm is not None:
            dual_rows.append(
                {"h3_id": h3, "osm_id": int(dual_osm), "km": dual_km}
            )

    counts = {"nearest_line_edges": 0, "dual_feed_edges": 0}

    nearest_cypher = (
        f"UNWIND $rows AS r "
        f"MATCH (c:{N.CELL} {{h3_id: r.h3_id}}), (l:{N.LINE} {{osm_id: r.osm_id}}) "
        f"MERGE (c)-[e:{E.NEAREST_LINE}]->(l) "
        f"SET e.km = r.km"
    )
    if nearest_rows:
        with batched_write(E.NEAREST_LINE, nearest_rows, batch_size=BATCH) as chunks:
            for chunk in chunks:
                query(nearest_cypher, {"rows": chunk})
                counts["nearest_line_edges"] += len(chunk)

    dual_cypher = (
        f"UNWIND $rows AS r "
        f"MATCH (c:{N.CELL} {{h3_id: r.h3_id}}), (l:{N.LINE} {{osm_id: r.osm_id}}) "
        f"MERGE (c)-[e:{E.DUAL_FEED_LINE}]->(l) "
        f"SET e.km = r.km"
    )
    if dual_rows:
        with batched_write(E.DUAL_FEED_LINE, dual_rows, batch_size=BATCH) as chunks:
            for chunk in chunks:
                query(dual_cypher, {"rows": chunk})
                counts["dual_feed_edges"] += len(chunk)

    return counts


def project_power() -> dict[str, int]:
    subs = _load_substations()
    lines = _load_lines()
    log.info("graph.projector.power.loaded", n_subs=len(subs), n_lines=len(lines))

    counts: dict[str, int] = {}
    counts["substations"] = _upsert_substations(subs)
    counts["lines"] = _upsert_lines(lines)
    counts["subgrids"] = _upsert_subgrids(lines)
    counts["line_in_subgrid"] = _link_lines_to_subgrids(lines)
    counts["parallel_pairs"] = _link_parallel(lines)
    n_connects, n_sub_subgrid = _link_connects(subs, lines)
    counts["connects"] = n_connects
    counts["sub_in_subgrid"] = n_sub_subgrid
    counts.update(_link_cells_to_nearest_lines(7))
    return counts

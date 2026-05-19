"""Sub-grid component labeling for HV transmission lines.

THIS IS THE TIER-4 DIFFERENTIATOR. Without it, "2nd-nearest power line"
returns a parallel circuit on the same tower as the 1st — a single tornado
kills both, and the Uptime Institute audit fails.

Algorithm (validated by Q3 of the plan-mode review):
  1. Pull raw_power_lines + raw_substations from Postgres.
  2. Cluster *parallel* line segments (within ~500m) into DBSCAN clusters —
     these are the same right-of-way and count as a single feed.
  3. Build a graph where:
        - nodes = substations (Points)
        - edges = a line cluster that touches >=1 substation on each end
     For lines with no substation endpoint match, snap line endpoints to
     the nearest substation within 1km; otherwise treat the line as its
     own dangling component.
  4. Run networkx.connected_components(G). Each component is an
     "independent sub-grid" — a power outage in one does not (in topology)
     propagate to another.
  5. Write the component id back to raw_power_lines.subgrid_component.

Result: ``nearest_hv_line_distinct_subgrid_km`` is the nearest line whose
subgrid_component differs from the nearest-overall line's component.
"""
from __future__ import annotations

import networkx as nx
from shapely.geometry import LineString, Point
from shapely.wkt import loads as wkt_loads
from sqlalchemy import text
from tqdm import tqdm

from app.core.config import load_pipeline_config
from app.core.db import session_scope  # noqa: F401 — used below for the SQL writes
from app.core.logging import get_logger
from app.ingest.osm.power import parallel_circuit_cluster

log = get_logger("ingest.osm.power_topology")


def _km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    R = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _load_lines() -> list[tuple[int, int | None, LineString]]:
    with session_scope() as session:
        rows = session.execute(
            text(
                """
                SELECT id, cluster_id, ST_AsText(geom)
                FROM dc_india.raw_power_lines
                """
            )
        ).all()
    return [(int(r[0]), (int(r[1]) if r[1] is not None else None), wkt_loads(r[2])) for r in rows]


def _load_substations() -> list[tuple[int, Point]]:
    with session_scope() as session:
        rows = session.execute(
            text("SELECT id, ST_AsText(geom) FROM dc_india.raw_substations")
        ).all()
    return [(int(r[0]), wkt_loads(r[1])) for r in rows]


def label_subgrid_components() -> int:
    """Assign subgrid_component to every raw_power_lines row.

    Returns the number of lines updated.
    """
    cfg = load_pipeline_config()["power_topology"]
    snap_km = float(cfg["substation_snap_km"])
    snap_deg = float(cfg["substation_snap_deg"])

    # Make sure parallel circuits are already clustered.
    parallel_circuit_cluster()

    lines = _load_lines()
    subs = _load_substations()
    log.info("topology.loaded", lines=len(lines), substations=len(subs))

    if not subs:
        # No substations to anchor topology — every cluster is its own component.
        log.warning("topology.no_substations", note="Falling back to cluster_id as component.")
        with session_scope() as session:
            session.execute(
                text(
                    """
                    UPDATE dc_india.raw_power_lines
                    SET subgrid_component = COALESCE(cluster_id, id)
                    """
                )
            )
        return len(lines)

    # Build a spatial index over substations for fast endpoint snap.
    from shapely.strtree import STRtree

    sub_geoms = [g for _, g in subs]
    sub_ids = [i for i, _ in subs]
    tree = STRtree(sub_geoms)

    def _snap_endpoint(pt: Point) -> int | None:
        candidates = tree.query(pt.buffer(snap_deg))
        nearest_id: int | None = None
        nearest_km = float("inf")
        for idx in candidates:
            sub = sub_geoms[idx]
            km = _km_between(pt.y, pt.x, sub.y, sub.x)
            if km <= snap_km and km < nearest_km:
                nearest_km = km
                nearest_id = sub_ids[idx]
        return nearest_id

    # Graph: nodes are substations + synthetic "cluster_<cid>" placeholders
    # for line clusters with no substation endpoints (dangling segments).
    G: nx.Graph = nx.Graph()
    for sid in sub_ids:
        G.add_node(f"sub_{sid}")

    # Group lines by cluster_id (parallel circuits act as one feed).
    cluster_map: dict[int, list[LineString]] = {}
    line_cluster: dict[int, int] = {}
    for line_id, cluster_id, geom in lines:
        cid = cluster_id if cluster_id is not None else -line_id  # singletons get negative ids
        cluster_map.setdefault(cid, []).append(geom)
        line_cluster[line_id] = cid

    for cid, geoms in tqdm(cluster_map.items(), desc="topology.clusters"):
        # Union the cluster's endpoints — first/last point of each line.
        endpoints: list[Point] = []
        for g in geoms:
            coords = list(g.coords)
            endpoints.append(Point(*coords[0]))
            endpoints.append(Point(*coords[-1]))

        # Snap each endpoint to a substation if within range.
        snapped_subs = {sid for pt in endpoints if (sid := _snap_endpoint(pt)) is not None}

        cluster_node = f"cluster_{cid}"
        G.add_node(cluster_node)
        for sid in snapped_subs:
            G.add_edge(cluster_node, f"sub_{sid}")

    # Connected components → component_id.
    component_of: dict[str, int] = {}
    for idx, comp in enumerate(nx.connected_components(G)):
        for node in comp:
            component_of[node] = idx

    # Map each line → cluster_node → component.
    updates: list[tuple[int, int]] = []
    for line_id, cid in line_cluster.items():
        node = f"cluster_{cid}"
        comp = component_of.get(node, cid + 1_000_000)  # fall back to cluster as own component
        updates.append((line_id, int(comp)))

    log.info(
        "topology.components",
        n_components=len({c for _, c in updates}),
        n_lines=len(updates),
    )

    # Bulk UPDATE via temp staging table + JOIN. O(n) round-trips becomes 1.
    with session_scope() as session:
        session.execute(
            text(
                """
                CREATE TEMP TABLE _topo_updates (
                    line_id BIGINT PRIMARY KEY,
                    subgrid_component INT NOT NULL
                ) ON COMMIT DROP
                """
            )
        )
        # Bulk COPY-style insert via executemany.
        session.execute(
            text("INSERT INTO _topo_updates (line_id, subgrid_component) VALUES (:line_id, :comp)"),
            [{"line_id": lid, "comp": comp} for lid, comp in updates],
        )
        session.execute(
            text(
                """
                UPDATE dc_india.raw_power_lines p
                SET subgrid_component = u.subgrid_component
                FROM _topo_updates u
                WHERE p.id = u.line_id
                """
            )
        )

    return len(updates)

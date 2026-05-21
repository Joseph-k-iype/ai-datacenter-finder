"""Stakeholder projector (P7).

Everything in this projector is derived from data already in PostGIS —
no curated CSVs, no out-of-band files. Sources:

  - Operators: distinct values of ``raw_power_lines.operator`` and
    ``raw_substations.operator`` (OSM-sourced; refreshed by power ingest).
    Classified into psu/state/private/unknown via a keyword heuristic
    that's data-driven (re-runs reapply the classification).
  - SEZs: ``dc_india.raw_sez`` (OSM via ``app/ingest/osm/sez.py``).
  - Data centers / hyperscalers: ``dc_india.raw_data_centers`` (OSM via
    ``app/ingest/osm/data_centers.py``). The label is :Hyperscaler since
    we treat any catalogued data center footprint as an adjacency target.

Writes:
    (:Operator {name})
    (:SEZ {sez_id})
    (:Hyperscaler {name})
    (:Line)-[:OPERATED_BY]->(:Operator)
    (:Substation)-[:OPERATED_BY]->(:Operator)

Spatial edges (Cell-[:INSIDE]->SEZ, Cell-[:HAS_HYPERSCALER]) are NOT
written here — they belong to the cells projector, which has the cell
geometry and can call PostGIS for the intersection. We separate node
upserts (cheap, no spatial work) from edge creation (expensive).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.core.db import session_scope
from app.core.logging import get_logger
from app.graph.client import batched_write, default_batch_size, query
from app.graph.schema import E, N

log = get_logger("graph.projector.stakeholder")

BATCH = default_batch_size()


# ---------------------------------------------------------------------------
# Operator classification — keyword heuristic. Stored *in code* (not a
# CSV) so re-runs always pick up new keywords when OSM evolves. If we
# want richer classification later, swap in a public DPIIT/CERC dataset
# ingested as just another raw_* table.
# ---------------------------------------------------------------------------
_PSU_KEYWORDS = (
    "powergrid", "power grid", "pgcil",
    "central transmission", "ntpc",
    "neyveli", "damodar valley", "dvc",
    "north eastern electric", "neepco",
    "sjvn", "thdc",
)
_STATE_KEYWORDS = (
    "transco", "discom", "vidyut", "mahatransco", "ptcl", "tneb",
    "transmission corporation", "state electricity", "seb",
    "kptcl", "mpptcl", "wbsedcl", "apgenco", "tsgenco",
)
_PRIVATE_KEYWORDS = (
    "adani", "reliance", "torrent", "tata power", "sterlite",
    "essel infra", "jindal", "vedanta",
)


def _classify(raw_name: str) -> tuple[str, str]:
    """Return (canonical_name, type) — type ∈ {'psu', 'state', 'private', 'unknown'}."""
    s = (raw_name or "").strip().lower()
    if not s:
        return (raw_name or "").strip(), "unknown"
    for kw in _PSU_KEYWORDS:
        if kw in s:
            return raw_name.strip(), "psu"
    for kw in _STATE_KEYWORDS:
        if kw in s:
            return raw_name.strip(), "state"
    for kw in _PRIVATE_KEYWORDS:
        if kw in s:
            return raw_name.strip(), "private"
    return raw_name.strip(), "unknown"


# ---------------------------------------------------------------------------
# Operators (from raw_power_lines + raw_substations)
# ---------------------------------------------------------------------------
def _project_operators() -> dict[str, int]:
    counts = {"operators": 0, "operator_line_edges": 0, "operator_sub_edges": 0}
    with session_scope() as session:
        line_ops = session.execute(
            text(
                """
                SELECT osm_id, operator
                FROM dc_india.raw_power_lines
                WHERE operator IS NOT NULL AND operator <> ''
                """
            )
        ).all()
        sub_ops = session.execute(
            text(
                """
                SELECT osm_id, operator
                FROM dc_india.raw_substations
                WHERE operator IS NOT NULL AND operator <> ''
                """
            )
        ).all()

    seen: dict[str, str] = {}
    for _, op in line_ops + sub_ops:
        if not op:
            continue
        name, kind = _classify(op)
        if not name:
            continue
        seen[name] = kind

    if not seen:
        return counts
    op_rows = [{"name": n, "type": k} for n, k in seen.items()]
    query(
        f"UNWIND $rows AS r "
        f"MERGE (o:{N.OPERATOR} {{name: r.name}}) "
        f"SET o.type = r.type",
        {"rows": op_rows},
    )
    counts["operators"] = len(op_rows)

    line_link_rows: list[dict[str, Any]] = []
    for osm_id, op in line_ops:
        if not op:
            continue
        name, _ = _classify(op)
        if name and osm_id is not None:
            line_link_rows.append({"osm_id": int(osm_id), "name": name})
    if line_link_rows:
        cypher = (
            f"UNWIND $rows AS r "
            f"MATCH (l:{N.LINE} {{osm_id: r.osm_id}}), (o:{N.OPERATOR} {{name: r.name}}) "
            f"MERGE (l)-[:{E.OPERATED_BY}]->(o)"
        )
        with batched_write("Line->Operator", line_link_rows, batch_size=BATCH) as chunks:
            for chunk in chunks:
                query(cypher, {"rows": chunk})
                counts["operator_line_edges"] += len(chunk)

    sub_link_rows: list[dict[str, Any]] = []
    for osm_id, op in sub_ops:
        if not op:
            continue
        name, _ = _classify(op)
        if name and osm_id is not None:
            sub_link_rows.append({"osm_id": int(osm_id), "name": name})
    if sub_link_rows:
        cypher = (
            f"UNWIND $rows AS r "
            f"MATCH (s:{N.SUBSTATION} {{osm_id: r.osm_id}}), (o:{N.OPERATOR} {{name: r.name}}) "
            f"MERGE (s)-[:{E.OPERATED_BY}]->(o)"
        )
        with batched_write("Substation->Operator", sub_link_rows, batch_size=BATCH) as chunks:
            for chunk in chunks:
                query(cypher, {"rows": chunk})
                counts["operator_sub_edges"] += len(chunk)

    return counts


# ---------------------------------------------------------------------------
# SEZs (from raw_sez)
# ---------------------------------------------------------------------------
def _project_sez() -> int:
    try:
        with session_scope() as session:
            rows = session.execute(
                text(
                    """
                    SELECT id, osm_id, name, operator, policy_tag, state_code,
                           ST_X(centroid) AS lon, ST_Y(centroid) AS lat
                    FROM dc_india.raw_sez
                    """
                )
            ).all()
    except ProgrammingError as exc:
        log.warning("graph.projector.stakeholder.sez.missing_table", error=str(exc))
        return 0

    payload = []
    for r in rows:
        # Use a stable sez_id: prefer OSM id, fall back to row id (always set).
        sez_id = f"osm:{r[1]}" if r[1] is not None else f"row:{r[0]}"
        payload.append(
            {
                "sez_id": sez_id,
                "name": r[2],
                "operator": r[3],
                "policy_tag": r[4],
                "state_code": r[5],
                "lon": float(r[6]) if r[6] is not None else None,
                "lat": float(r[7]) if r[7] is not None else None,
            }
        )

    if not payload:
        return 0
    cypher = (
        f"UNWIND $rows AS r "
        f"MERGE (s:{N.SEZ} {{sez_id: r.sez_id}}) "
        f"SET s.name = r.name, s.operator = r.operator, "
        f"    s.policy_tag = r.policy_tag, s.state_code = r.state_code, "
        f"    s.lat = r.lat, s.lon = r.lon"
    )
    with batched_write(N.SEZ, payload, batch_size=BATCH) as chunks:
        for chunk in chunks:
            query(cypher, {"rows": chunk})
    return len(payload)


# ---------------------------------------------------------------------------
# Data centers / Hyperscalers (from raw_data_centers)
# ---------------------------------------------------------------------------
def _project_hyperscalers() -> int:
    try:
        with session_scope() as session:
            rows = session.execute(
                text(
                    """
                    SELECT id, osm_id, name, operator, company, tier,
                           city, state_code,
                           ST_X(geom) AS lon, ST_Y(geom) AS lat
                    FROM dc_india.raw_data_centers
                    """
                )
            ).all()
    except ProgrammingError as exc:
        log.warning(
            "graph.projector.stakeholder.data_centers.missing_table",
            error=str(exc),
        )
        return 0

    payload = []
    for r in rows:
        # Stable key: prefer OSM id; row id is always present as a fallback.
        name_key = f"osm:{r[1]}" if r[1] is not None else f"row:{r[0]}"
        payload.append(
            {
                "name": name_key,
                "display_name": r[2],
                "operator": r[3],
                "company": r[4],
                "tier": int(r[5]) if r[5] is not None else None,
                "city": r[6],
                "state_code": r[7],
                "lon": float(r[8]) if r[8] is not None else None,
                "lat": float(r[9]) if r[9] is not None else None,
            }
        )

    if not payload:
        return 0
    cypher = (
        f"UNWIND $rows AS r "
        f"MERGE (h:{N.HYPERSCALER} {{name: r.name}}) "
        f"SET h.display_name = r.display_name, h.operator = r.operator, "
        f"    h.company = r.company, h.tier = r.tier, h.city = r.city, "
        f"    h.state_code = r.state_code, h.lat = r.lat, h.lon = r.lon"
    )
    with batched_write(N.HYPERSCALER, payload, batch_size=BATCH) as chunks:
        for chunk in chunks:
            query(cypher, {"rows": chunk})
    return len(payload)


def project_stakeholder() -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        counts.update(_project_operators())
    except ProgrammingError as exc:
        log.warning("graph.projector.stakeholder.operators.unavailable", error=str(exc))
    counts["sez"] = _project_sez()
    counts["hyperscalers"] = _project_hyperscalers()
    return counts

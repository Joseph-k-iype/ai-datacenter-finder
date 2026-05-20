"""Parity checks between Postgres (source of truth) and FalkorDB.

For each entity that lives in both stores, count rows in Postgres and
nodes in the graph. Drift indicates a missing projection, a stale
rebuild, or data corruption — surface it loudly via ``dc graph parity``
and the nightly cron job.

The check is intentionally count-based and not row-by-row. A row diff
on 600k cells is too expensive for a routine check; counts catch the
common drift modes (a full table missing, a bulk delete missed by the
graph) which is what we actually need to detect.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.core.db import session_scope
from app.core.logging import get_logger
from app.graph.client import query_rows
from app.graph.schema import N

log = get_logger("graph.parity")


# (postgres_select, graph_match_label) pairs. SELECT must return a single
# count(*) column.
_PARITY: list[tuple[str, str, str]] = [
    ("h3_cells_res7", "SELECT count(*) FROM dc_india.h3_cells_res7", N.CELL),
    ("india_states", "SELECT count(*) FROM dc_india.india_states", N.STATE),
    ("raw_substations", "SELECT count(*) FROM dc_india.raw_substations", N.SUBSTATION),
    ("raw_power_lines", "SELECT count(*) FROM dc_india.raw_power_lines", N.LINE),
    ("ingestion_runs", "SELECT count(*) FROM dc_india.ingestion_runs", N.INGESTION_RUN),
]


def _pg_count(sql: str) -> int | None:
    try:
        with session_scope() as session:
            row = session.execute(text(sql)).first()
        return int(row[0]) if row else 0
    except ProgrammingError as exc:
        log.warning("graph.parity.pg.table_missing", error=str(exc))
        return None


def _graph_count(label: str, *, where: str | None = None) -> int:
    cypher = f"MATCH (n:{label})"
    if where:
        cypher += f" WHERE {where}"
    cypher += " RETURN count(n)"
    rows = query_rows(cypher)
    return int(rows[0][0]) if rows else 0


def run_parity_checks(tolerance_pct: float = 0.5) -> dict[str, Any]:
    """Compare counts. Return a structured report. ``drift=true`` on diff > tol."""
    results: list[dict[str, Any]] = []
    any_drift = False

    for name, pg_sql, label in _PARITY:
        pg = _pg_count(pg_sql)
        # For Cell we restrict to resolution 7 because the graph holds
        # res-7 and res-8 mixed under the same label.
        where = "n.resolution = 7" if label == N.CELL else None
        graph = _graph_count(label, where=where)
        diff = None
        drift = False
        if pg is not None:
            diff = graph - pg
            denom = pg if pg > 0 else 1
            drift = abs(diff) / denom > (tolerance_pct / 100.0)
            any_drift = any_drift or drift
        results.append(
            {
                "name": name,
                "label": label,
                "postgres": pg,
                "graph": graph,
                "diff": diff,
                "drift": drift,
            }
        )

    summary = {
        "tolerance_pct": tolerance_pct,
        "drift": any_drift,
        "checks": results,
    }
    log.info("graph.parity.complete", drift=any_drift, checks=len(results))
    return summary

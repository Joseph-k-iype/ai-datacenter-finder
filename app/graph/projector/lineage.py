"""Lineage projector (P3).

Project the governance tables — ingestion_runs, schema_contracts,
dead_letter_queue — into a graph so any score can trace back to its
upstream inputs via a single Cypher path traversal.

Reads from:
    dc_india.ingestion_runs
    dc_india.schema_contracts
    dc_india.dead_letter_queue

Writes:
    (:IngestionRun {run_id})
    (:SchemaContract {schema_hash})
    (:RejectedRow {dlq_id})
    (:IngestionRun)-[:VALIDATED_BY]->(:SchemaContract)
    (:RejectedRow)-[:REJECTED_BY]->(:IngestionRun)

The :FROM edges from every raw_* row back to its IngestionRun are
written by the per-source projectors (cells.py, power.py) once row-level
linkage is needed in the UI. Keeping that out of this module avoids
scanning every raw table on a lineage-only rebuild.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.core.db import session_scope
from app.core.logging import get_logger
from app.graph.client import batched_write, query
from app.graph.schema import E, N

log = get_logger("graph.projector.lineage")

BATCH = 1000


def _project_schema_contracts() -> int:
    try:
        with session_scope() as session:
            rows = session.execute(
                text(
                    """
                    SELECT schema_hash, source, version, payload, created_at
                    FROM dc_india.schema_contracts
                    """
                )
            ).all()
    except ProgrammingError as exc:
        log.warning("graph.projector.lineage.schema_contracts.missing", error=str(exc))
        return 0

    payload = [
        {
            "schema_hash": r[0],
            "source": r[1],
            "version": r[2],
            # payload may be JSONB; coerce to string for the graph property
            "payload_json": (
                json.dumps(r[3], default=str)
                if isinstance(r[3], (dict, list))
                else (r[3] if r[3] is not None else None)
            ),
            "created_at": r[4].isoformat() if r[4] is not None else None,
        }
        for r in rows
    ]
    if not payload:
        return 0
    cypher = (
        f"UNWIND $rows AS r "
        f"MERGE (sc:{N.SCHEMA_CONTRACT} {{schema_hash: r.schema_hash}}) "
        f"SET sc.source = r.source, sc.version = r.version, "
        f"    sc.payload_json = r.payload_json, sc.created_at = r.created_at"
    )
    with batched_write(N.SCHEMA_CONTRACT, payload, batch_size=BATCH) as chunks:
        for chunk in chunks:
            query(cypher, {"rows": chunk})
    return len(payload)


def _project_ingestion_runs() -> int:
    with session_scope() as session:
        rows = session.execute(
            text(
                """
                SELECT run_id, source, status, schema_hash, upstream_source,
                       started_at, finished_at, row_count, rows_rejected,
                       duration_seconds, notes
                FROM dc_india.ingestion_runs
                """
            )
        ).all()

    payload = [
        {
            "run_id": str(r[0]),
            "source": r[1],
            "status": r[2],
            "schema_hash": r[3],
            "upstream_source": r[4],
            "started_at": r[5].isoformat() if r[5] is not None else None,
            "finished_at": r[6].isoformat() if r[6] is not None else None,
            "row_count": int(r[7]) if r[7] is not None else 0,
            "rows_rejected": int(r[8]) if r[8] is not None else 0,
            "duration_seconds": float(r[9]) if r[9] is not None else None,
            "notes": r[10],
        }
        for r in rows
    ]
    if not payload:
        return 0

    upsert = (
        f"UNWIND $rows AS r "
        f"MERGE (ir:{N.INGESTION_RUN} {{run_id: r.run_id}}) "
        f"SET ir.source = r.source, ir.status = r.status, "
        f"    ir.schema_hash = r.schema_hash, "
        f"    ir.upstream_source = r.upstream_source, "
        f"    ir.started_at = r.started_at, ir.finished_at = r.finished_at, "
        f"    ir.row_count = r.row_count, ir.rows_rejected = r.rows_rejected, "
        f"    ir.duration_seconds = r.duration_seconds, ir.notes = r.notes"
    )
    link_contract = (
        f"UNWIND $rows AS r "
        f"MATCH (ir:{N.INGESTION_RUN} {{run_id: r.run_id}}), "
        f"      (sc:{N.SCHEMA_CONTRACT} {{schema_hash: r.schema_hash}}) "
        f"MERGE (ir)-[:{E.VALIDATED_BY}]->(sc)"
    )

    with batched_write(N.INGESTION_RUN, payload, batch_size=BATCH) as chunks:
        for chunk in chunks:
            query(upsert, {"rows": chunk})
            # Only link runs that recorded a schema_hash.
            linkables = [c for c in chunk if c["schema_hash"]]
            if linkables:
                query(link_contract, {"rows": linkables})
    return len(payload)


def _project_rejected_rows() -> int:
    try:
        with session_scope() as session:
            rows = session.execute(
                text(
                    """
                    SELECT id, run_id, source, error, payload, occurred_at
                    FROM dc_india.dead_letter_queue
                    """
                )
            ).all()
    except ProgrammingError as exc:
        log.warning("graph.projector.lineage.dlq.missing", error=str(exc))
        return 0

    payload: list[dict[str, Any]] = [
        {
            # dlq_id is the graph-side natural key; in Postgres it's the
            # bigserial `id`. Prefixed so it never collides with osm_id etc.
            "dlq_id": f"dlq:{r[0]}",
            "run_id": str(r[1]) if r[1] is not None else None,
            "source": r[2],
            # Truncate to a sane upper bound — full payload stays in Postgres.
            "error": (r[3] or "")[:500],
            "payload_preview": (
                json.dumps(r[4], default=str)[:1000]
                if isinstance(r[4], (dict, list))
                else (str(r[4])[:1000] if r[4] is not None else None)
            ),
            "occurred_at": r[5].isoformat() if r[5] is not None else None,
        }
        for r in rows
    ]
    if not payload:
        return 0
    upsert = (
        f"UNWIND $rows AS r "
        f"MERGE (rr:{N.REJECTED_ROW} {{dlq_id: r.dlq_id}}) "
        f"SET rr.source = r.source, rr.error = r.error, "
        f"    rr.payload_preview = r.payload_preview, "
        f"    rr.occurred_at = r.occurred_at"
    )
    link_run = (
        f"UNWIND $rows AS r "
        f"MATCH (rr:{N.REJECTED_ROW} {{dlq_id: r.dlq_id}}), "
        f"      (ir:{N.INGESTION_RUN} {{run_id: r.run_id}}) "
        f"MERGE (rr)-[:{E.REJECTED_BY}]->(ir)"
    )
    with batched_write(N.REJECTED_ROW, payload, batch_size=BATCH) as chunks:
        for chunk in chunks:
            query(upsert, {"rows": chunk})
            linkables = [c for c in chunk if c["run_id"]]
            if linkables:
                query(link_run, {"rows": linkables})
    return len(payload)


def project_lineage() -> dict[str, int]:
    counts: dict[str, int] = {}
    counts["schema_contracts"] = _project_schema_contracts()
    counts["ingestion_runs"] = _project_ingestion_runs()
    counts["rejected_rows"] = _project_rejected_rows()
    return counts


# ---------------------------------------------------------------------------
# Incremental hooks — called from ``app/governance/lineage.py``'s
# ingestion_run context manager when ``FALKORDB_AUTO_SYNC`` is true.
# ---------------------------------------------------------------------------
def upsert_ingestion_run_node(
    *,
    run_id: str,
    source: str,
    status: str,
    schema_hash: str | None,
    upstream_source: str | None,
    started_at: str | None = None,
    finished_at: str | None = None,
    row_count: int = 0,
    rows_rejected: int = 0,
    duration_seconds: float | None = None,
    notes: str | None = None,
) -> None:
    """Upsert a single IngestionRun + (optionally) link to its SchemaContract.

    Called twice per ingest — once at start (status='running') and once
    on exit (status='success'/'failed'). Idempotent via MERGE.
    """
    rows = [
        {
            "run_id": run_id,
            "source": source,
            "status": status,
            "schema_hash": schema_hash,
            "upstream_source": upstream_source,
            "started_at": started_at,
            "finished_at": finished_at,
            "row_count": row_count,
            "rows_rejected": rows_rejected,
            "duration_seconds": duration_seconds,
            "notes": notes,
        }
    ]
    query(
        f"UNWIND $rows AS r "
        f"MERGE (ir:{N.INGESTION_RUN} {{run_id: r.run_id}}) "
        f"SET ir.source = r.source, ir.status = r.status, "
        f"    ir.schema_hash = r.schema_hash, "
        f"    ir.upstream_source = r.upstream_source, "
        f"    ir.started_at = COALESCE(ir.started_at, r.started_at), "
        f"    ir.finished_at = r.finished_at, "
        f"    ir.row_count = r.row_count, "
        f"    ir.rows_rejected = r.rows_rejected, "
        f"    ir.duration_seconds = r.duration_seconds, "
        f"    ir.notes = r.notes",
        {"rows": rows},
    )
    if schema_hash:
        query(
            f"MATCH (ir:{N.INGESTION_RUN} {{run_id: $run_id}}), "
            f"      (sc:{N.SCHEMA_CONTRACT} {{schema_hash: $schema_hash}}) "
            f"MERGE (ir)-[:{E.VALIDATED_BY}]->(sc)",
            {"run_id": run_id, "schema_hash": schema_hash},
        )

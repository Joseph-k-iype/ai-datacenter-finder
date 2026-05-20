"""IngestionRun context manager. Opens a row in ingestion_runs at start, closes at end.

Usage:
    with ingestion_run(source="osm.power") as run:
        # work; run.run_id is the UUID, attach it to every output row
        run.row_count = len(df)
"""
from __future__ import annotations

import time
import traceback
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import text

from app.core.config import load_pipeline_config
from app.core.db import session_scope
from app.core.logging import get_logger

log = get_logger("governance.lineage")


def has_recent_success(
    source: str,
    *,
    ttl_hours: int,
) -> uuid.UUID | None:
    """Return the run_id of a recent successful run for ``source``, or None.

    Used by the per-layer ``should_skip`` guard to make ingestion idempotent
    across re-runs: if the same source completed successfully within
    ``ttl_hours``, the new call returns immediately.

    The TTL cutoff is computed in Python (not in SQL) because SQLAlchemy's
    ``text()`` parser mishandles ``:hours::text || ' hours'`` — the ``::``
    cast operator next to a bind parameter confuses its colon-escape logic
    and the parameter never gets substituted. Passing a timestamp directly
    sidesteps the issue entirely.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=int(ttl_hours))
    with session_scope() as session:
        row = session.execute(
            text(
                """
                SELECT run_id FROM dc_india.ingestion_runs
                WHERE source = :source
                  AND status = 'success'
                  AND finished_at IS NOT NULL
                  AND finished_at > :cutoff
                ORDER BY finished_at DESC
                LIMIT 1
                """
            ),
            {"source": source, "cutoff": cutoff},
        ).first()
    if row is None:
        return None
    raw = row[0]
    return raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))


def should_skip(
    source: str,
    *,
    fresh: bool,
    ttl_hours: int | None = None,
) -> uuid.UUID | None:
    """Return the existing run_id if this ingest should be skipped.

    Parameters
    ----------
    source
        Logical source identifier (e.g. ``'gee.flood'``, ``'osm.power_lines'``).
    fresh
        If True, the user explicitly asked to bypass the skip guard.
    ttl_hours
        Override the configured TTL. Default from
        ``configs/pipeline.yml::ingestion.skip_ttl_hours``.
    """
    if fresh:
        return None
    if ttl_hours is None:
        ttl_hours = int(
            load_pipeline_config().get("ingestion", {}).get("skip_ttl_hours", 24)
        )
    return has_recent_success(source, ttl_hours=ttl_hours)


@dataclass
class IngestionRunCtx:
    run_id: uuid.UUID
    source: str
    row_count: int = 0
    rows_rejected: int = 0
    schema_hash: str | None = None
    upstream_source: str | None = None
    notes: list[str] = field(default_factory=list)


@contextmanager
def ingestion_run(
    source: str,
    upstream_source: str | None = None,
    schema_hash: str | None = None,
) -> Iterator[IngestionRunCtx]:
    run_id = uuid.uuid4()
    ctx = IngestionRunCtx(
        run_id=run_id,
        source=source,
        schema_hash=schema_hash,
        upstream_source=upstream_source,
    )
    started = time.monotonic()
    structlog.contextvars.bind_contextvars(pipeline_run_id=str(run_id), source=source)
    log.info("ingest.start", upstream=upstream_source)

    with session_scope() as session:
        session.execute(
            text(
                """
                INSERT INTO dc_india.ingestion_runs
                  (run_id, source, status, schema_hash, upstream_source)
                VALUES (:run_id, :source, 'running', :schema_hash, :upstream)
                """
            ),
            {
                "run_id": str(run_id),
                "source": source,
                "schema_hash": schema_hash,
                "upstream": upstream_source,
            },
        )

    status = "success"
    error_note: str | None = None
    try:
        yield ctx
    except Exception as exc:
        status = "failed"
        error_note = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[:2000]}"
        log.error("ingest.fail", error=str(exc))
        raise
    finally:
        duration = time.monotonic() - started
        notes = "\n".join(ctx.notes + ([error_note] if error_note else []))
        with session_scope() as session:
            session.execute(
                text(
                    """
                    UPDATE dc_india.ingestion_runs
                    SET finished_at = now(),
                        status = :status,
                        row_count = :row_count,
                        rows_rejected = :rows_rejected,
                        duration_seconds = :duration,
                        notes = :notes
                    WHERE run_id = :run_id
                    """
                ),
                {
                    "status": status,
                    "row_count": ctx.row_count,
                    "rows_rejected": ctx.rows_rejected,
                    "duration": duration,
                    "notes": notes or None,
                    "run_id": str(run_id),
                },
            )
        log.info(
            "ingest.end",
            status=status,
            rows=ctx.row_count,
            rejected=ctx.rows_rejected,
            duration_seconds=round(duration, 2),
        )
        structlog.contextvars.unbind_contextvars("pipeline_run_id", "source")

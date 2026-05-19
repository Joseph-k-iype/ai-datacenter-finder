"""Dead-letter queue writer. Bad rows go here instead of aborting the run."""
from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text

from app.core.db import session_scope
from app.core.logging import get_logger

log = get_logger("governance.dlq")


def push(
    run_id: uuid.UUID | str,
    source: str,
    payload: dict[str, Any],
    error: str,
) -> None:
    """Push a single rejected row to the DLQ."""
    with session_scope() as session:
        session.execute(
            text(
                """
                INSERT INTO dc_india.dead_letter_queue (run_id, source, payload, error)
                VALUES (:run_id, :source, CAST(:payload AS JSONB), :error)
                """
            ),
            {
                "run_id": str(run_id),
                "source": source,
                "payload": json.dumps(payload, default=str),
                "error": error[:2000],
            },
        )


def push_many(
    run_id: uuid.UUID | str,
    source: str,
    rows: list[dict[str, Any]],
    error: str,
) -> int:
    if not rows:
        return 0
    with session_scope() as session:
        session.execute(
            text(
                """
                INSERT INTO dc_india.dead_letter_queue (run_id, source, payload, error)
                SELECT :run_id, :source, CAST(unnest(:payloads::text[]) AS JSONB), :error
                """
            ),
            {
                "run_id": str(run_id),
                "source": source,
                "payloads": [json.dumps(r, default=str) for r in rows],
                "error": error[:2000],
            },
        )
    log.warning("dlq.pushed", source=source, count=len(rows), error=error[:200])
    return len(rows)


def counts_by_source() -> dict[str, int]:
    with session_scope() as session:
        result = session.execute(
            text("SELECT source, COUNT(*) FROM dc_india.dead_letter_queue GROUP BY source")
        ).all()
    return {row[0]: int(row[1]) for row in result}

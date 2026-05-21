"""Dead-letter queue writer. Bad rows go here instead of aborting the run."""
from __future__ import annotations

import json
import math
import uuid
from typing import Any

from sqlalchemy import text

from app.core.db import session_scope
from app.core.logging import get_logger

log = get_logger("governance.dlq")


def _json_safe(value: Any) -> Any:
    """Recursively replace NaN / NA / inf with None so the JSONB column
    accepts the payload.

    Python's ``json.dumps`` emits the literal token ``NaN`` for ``float
    nan`` (because ``allow_nan`` defaults to True), which is *not* valid
    JSON and which PostgreSQL rejects with
    ``invalid input syntax for type json: Token "NaN" is invalid``.

    Identity test ``v != v`` is the dependency-free way to detect NaN —
    NaN is the only float value not equal to itself. ``math.isinf`` covers
    ±inf which JSON also can't represent.
    """
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    # Pandas pd.NA and numpy.nan compare unequal to themselves.
    try:
        if value != value:  # noqa: PLR0124 — NaN check
            return None
    except (TypeError, ValueError):
        pass
    return value


def _to_json(value: Any) -> str:
    """Dump a value to a JSONB-safe string."""
    return json.dumps(_json_safe(value), default=str)


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
                "payload": _to_json(payload),
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
    truncated_error = error[:2000]
    run_id_str = str(run_id)
    params = [
        {
            "run_id": run_id_str,
            "source": source,
            "payload": _to_json(r),
            "error": truncated_error,
        }
        for r in rows
    ]
    with session_scope() as session:
        session.execute(
            text(
                """
                INSERT INTO dc_india.dead_letter_queue (run_id, source, payload, error)
                VALUES (:run_id, :source, CAST(:payload AS JSONB), :error)
                """
            ),
            params,
        )
    log.warning("dlq.pushed", source=source, count=len(rows), error=error[:200])
    return len(rows)


def counts_by_source() -> dict[str, int]:
    with session_scope() as session:
        result = session.execute(
            text("SELECT source, COUNT(*) FROM dc_india.dead_letter_queue GROUP BY source")
        ).all()
    return {row[0]: int(row[1]) for row in result}

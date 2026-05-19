"""Shared utilities for all ingest adapters: validation, DLQ, run lineage."""
from __future__ import annotations

from typing import Any

import pandas as pd
from pandera.errors import SchemaErrors
from pandera.pandas import DataFrameSchema

from app.core.logging import get_logger
from app.governance import dlq

log = get_logger("ingest.base")


def validate_and_split(
    df: pd.DataFrame,
    schema: DataFrameSchema,
    *,
    run_id: str,
    source: str,
) -> tuple[pd.DataFrame, int]:
    """Validate ``df`` against ``schema``; route rejects to DLQ.

    Returns ``(clean_df, rejected_count)``.
    """
    if df.empty:
        return df, 0

    try:
        clean = schema.validate(df, lazy=True)
        return clean, 0
    except SchemaErrors as err:
        failure_cases = err.failure_cases
        bad_indices = sorted(set(failure_cases["index"].dropna().astype(int).tolist()))
        bad_rows = df.iloc[bad_indices] if bad_indices else pd.DataFrame()
        clean = df.drop(index=bad_indices, errors="ignore").reset_index(drop=True)
        if not bad_rows.empty:
            dlq.push_many(
                run_id=run_id,
                source=source,
                rows=bad_rows.to_dict(orient="records"),
                error=str(err)[:1000],
            )
        log.warning(
            "ingest.validation.partial",
            source=source,
            rejected=len(bad_rows),
            kept=len(clean),
        )
        return clean, len(bad_rows)


def chunked(seq: list[Any], size: int) -> list[list[Any]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]

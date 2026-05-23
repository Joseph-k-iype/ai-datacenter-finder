"""Common helpers shared by per-layer GEE modules."""
from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from app.core.db import bulk_execute
from app.core.logging import get_logger

log = get_logger("ingest.gee.common")


def _build_upsert_sql(table: str, columns: list[str]) -> str:
    cols_assign = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)
    insert_cols = ", ".join(columns)
    insert_params = ", ".join(f":{c}" for c in columns)
    return f"""
        INSERT INTO dc_india.{table}
          (h3_id, resolution, {insert_cols}, ingestion_run_id)
        VALUES
          (CAST(:h3_id AS h3index), :resolution, {insert_params}, :ingestion_run_id)
        ON CONFLICT (h3_id, resolution) DO UPDATE
          SET {cols_assign},
              ingestion_run_id = EXCLUDED.ingestion_run_id
    """


def _df_to_param_rows(
    df: pd.DataFrame,
    columns: list[str],
    resolution: int,
    run_id: str,
) -> Iterable[dict]:
    """Yield param dicts one at a time so we never materialise a giant
    list-of-dicts (the old code did ``df.to_dict(orient='records')`` then
    mutated each row, doubling memory). The generator backs a streaming
    ``executemany`` chunk loop in ``bulk_execute``.
    """
    cols = ["h3_id", *columns]
    values_iter = df[cols].itertuples(index=False, name=None)
    for tup in values_iter:
        row: dict = {"resolution": resolution, "ingestion_run_id": run_id}
        for k, v in zip(cols, tup, strict=False):
            # NaN/NA → None so the DB sees NULL, not a stringified 'nan'.
            if (isinstance(v, float) and v != v) or v is pd.NA:
                row[k] = None
            else:
                row[k] = v
        yield row


def upsert_zonal(
    *,
    df: pd.DataFrame,
    table: str,
    resolution: int,
    run_id: str,
    columns: list[str],
    chunk_size: int = 5000,
) -> int:
    """Bulk-upsert ``df`` into ``raster_zonal_<...>``.

    Streams rows (no full ``to_dict`` copy) and lets the downstream
    ``bulk_execute`` chunk them into executemany batches. Safe to call
    from a per-chunk ``on_chunk`` callback during a streaming GEE
    export — memory cost stays O(chunk_size).
    """
    if df.empty:
        return 0

    sql = _build_upsert_sql(table, columns)
    rows = list(_df_to_param_rows(df, columns, resolution, run_id))
    n = bulk_execute(sql, rows, chunk_size=chunk_size)
    log.info("zonal.upserted", table=table, n=n)
    return n

"""Common helpers shared by per-layer GEE modules."""
from __future__ import annotations

import pandas as pd

from app.core.db import bulk_execute
from app.core.logging import get_logger

log = get_logger("ingest.gee.common")


def upsert_zonal(
    *,
    df: pd.DataFrame,
    table: str,
    resolution: int,
    run_id: str,
    columns: list[str],
    chunk_size: int = 5000,
) -> int:
    """Bulk-upsert into one of the raster_zonal_* tables.

    Each row in ``df`` must contain ``h3_id`` (string) + every name in
    ``columns``. ``resolution`` and ``ingestion_run_id`` are filled in
    here, so callers don't need to mutate ``df`` first.
    """
    if df.empty:
        return 0

    cols_assign = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns)
    insert_cols = ", ".join(columns)
    insert_params = ", ".join(f":{c}" for c in columns)

    sql = f"""
        INSERT INTO dc_india.{table}
          (h3_id, resolution, {insert_cols}, ingestion_run_id)
        VALUES
          (CAST(:h3_id AS h3index), :resolution, {insert_params}, :ingestion_run_id)
        ON CONFLICT (h3_id, resolution) DO UPDATE
          SET {cols_assign},
              ingestion_run_id = EXCLUDED.ingestion_run_id
    """

    rows = df.to_dict(orient="records")
    for r in rows:
        r["resolution"] = resolution
        r["ingestion_run_id"] = run_id

    n = bulk_execute(sql, rows, chunk_size=chunk_size)
    log.info("zonal.upserted", table=table, n=n, chunks=(n // chunk_size) + 1)
    return n

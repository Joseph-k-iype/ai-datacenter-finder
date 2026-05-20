"""Chunked GEE zonal stats; parallel chunks with per-chunk Parquet cache.

Each layer's pipeline:

  1. Cells stream from Postgres (``h3_cells_res{R}``) in chunks of
     ``chunk_size`` (default 2000) — materialized once in memory.
  2. Already-cached chunks are skipped before any worker is spun up.
  3. Remaining chunks run **in parallel** via ``ThreadPoolExecutor``
     (default ``max_workers=6``). GEE's ``reduceRegions(...).getInfo()``
     is a blocking HTTP call that releases the GIL, so threads are the
     right primitive — no need for multiprocessing.
  4. Each chunk's result is written to
     ``data/interim/gee/{export_name}/res{R}/chunk_NNNNN.parquet`` —
     resumable: a re-run with the same chunk_size skips finished work.
  5. The driver returns the concatenated DataFrame ordered by chunk
     index (i.e. by h3_id since chunks are pulled with ``ORDER BY h3_id``).

Trade-off vs. GCS-async-export:
  + No GCS bucket / service-account JSON required.
  + Resumable on transient GEE errors (per-chunk granularity).
  + Single source of truth for cells (Postgres). No upfront asset
    publication needed; ``make push-grid-to-gee`` is optional.
  - GEE rate limits may push back at very high ``max_workers``; 6 is
    safe for free-tier accounts.

The previous version ran chunks strictly serially and took ~10 hours
end-to-end at pan-India scale; the parallel pipeline reduces this to
~30-90 minutes total across all 6 raster layers depending on settings.

Why threads, not asyncio?
-------------------------
``earthengine-api`` is built on ``googleapiclient`` → ``httplib2``,
which is **synchronous**. There is no native ``async`` EE client. Wrapping
its calls in ``asyncio.to_thread(...)`` would just run them on a
``ThreadPoolExecutor`` under the hood — identical performance, more
ceremony. To get true asyncio benefit we'd have to reimplement the EE
REST/Cloud-API client on top of ``httpx.AsyncClient``, a multi-week
project for ~zero gain because **the bottleneck is GEE-server compute
time, not Python's I/O scheduler**.

For our workload (network-I/O-bound, waiting on GEE compute), threads
and asyncio are throughput-equivalent. Threads also share memory cheaply
which matters for the in-memory cell list. We default to 10 chunk
workers; the M4 family handles 20+ without breaking a sweat — the
practical limit is GEE's per-account rate cap, not local CPU.

Why not multiprocessing?
------------------------
Each subprocess would need its own ``ee.Initialize()`` (~1.5 s) and its
own Postgres connection pool. IPC for the chunk results would dominate.
Threads share `ee` state and the SQLAlchemy engine for free.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import ee
import pandas as pd
from sqlalchemy import text

from app.core.config import PROJECT_ROOT, load_pipeline_config
from app.core.db import session_scope
from app.core.logging import get_logger
from app.ingest.gee.client import init_ee

log = get_logger("ingest.gee.zonal_export")


def _wkt_polygon_to_ee_geometry(wkt: str) -> ee.Geometry:
    """Parse a ``POLYGON((x1 y1, x2 y2, …))`` WKT into ``ee.Geometry.Polygon``.

    Lightweight: enough for H3 hex boundaries which are always single-ring
    polygons. For general WKT use shapely.wkt.loads + json.dumps.
    """
    inner = wkt[wkt.index("((") + 2 : wkt.rindex("))")]
    ring = [tuple(map(float, p.strip().split())) for p in inner.split(",")]
    return ee.Geometry.Polygon([ring], proj=None, geodesic=False)


def _iter_cell_chunks(resolution: int, chunk_size: int) -> Iterator[list[tuple[str, str]]]:
    """Yield successive lists of (h3_id, wkt) tuples from Postgres."""
    sql = text(
        f"""
        SELECT h3_id::text AS h3_id, ST_AsText(geom) AS wkt
        FROM dc_india.h3_cells_res{resolution}
        ORDER BY h3_id
        """
    )
    with session_scope() as session:
        result = session.execute(sql)
        chunk: list[tuple[str, str]] = []
        for row in result:
            chunk.append((row.h3_id, row.wkt))
            if len(chunk) >= chunk_size:
                yield chunk
                chunk = []
        if chunk:
            yield chunk


def _cache_dir_for(export_name: str, resolution: int) -> Path:
    cache_root = PROJECT_ROOT / "data" / "interim" / "gee" / export_name / f"res{resolution}"
    cache_root.mkdir(parents=True, exist_ok=True)
    return cache_root


def _properties_to_dataframe(info: dict, expected_h3_ids: list[str]) -> pd.DataFrame:
    """Convert a getInfo() FeatureCollection into a DataFrame.

    Cells whose reduction returned no data (e.g. ocean cells with no land
    pixels) appear as h3_id with NaN reduction columns.
    """
    feats = info.get("features", [])
    by_id: dict[str, dict] = {}
    for f in feats:
        props = dict(f.get("properties", {}))
        h3_id = props.pop("h3_id", None)
        if h3_id:
            by_id[h3_id] = props
    rows = [{"h3_id": h, **by_id.get(h, {})} for h in expected_h3_ids]
    return pd.DataFrame(rows)


def _format_eta(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}min"
    return f"{seconds / 3600:.1f}h"


def run_zonal_export(
    *,
    image: ee.Image,
    reducer: ee.Reducer,
    band_names: list[str],
    resolution: int,
    export_name: str,
    scale_m: int,
    crs: str = "EPSG:4326",
    chunk_size: int | None = None,
    tile_scale: int | None = None,
    max_workers: int | None = None,
    fresh: bool = False,
) -> pd.DataFrame:
    """Parallel chunked reduceRegions over the Postgres-resident H3 grid.

    Parameters
    ----------
    image, reducer, band_names, resolution, export_name, scale_m, crs
        See module docstring.
    chunk_size
        Cells per GEE request. Default ``configs/pipeline.yml::gee.export.chunk_size``.
    tile_scale
        Raise to 8 or 16 on "Computed value too large" errors.
    max_workers
        Parallel chunk workers. Default ``configs/pipeline.yml::gee.export.max_workers``
        (typically 6). Pass ``1`` for deterministic serial execution
        (used by tests).
    fresh
        If True, ignore existing chunk Parquets and re-fetch everything.
    """
    init_ee()
    cfg = load_pipeline_config()
    export_cfg = cfg["gee"].get("export", {})
    chunk_size = chunk_size or int(export_cfg.get("chunk_size", 2000))
    tile_scale = tile_scale or int(export_cfg.get("tile_scale", 4))
    max_workers = max_workers or int(export_cfg.get("max_workers", 6))

    cache_dir = _cache_dir_for(export_name, resolution)

    # Materialize all chunks upfront so workers don't fight over the
    # Postgres session. Memory cost ≈ chunk_size × #chunks × ~100 B per
    # WKT ≈ ~65 MB for pan-India res-7. Well within laptop RAM.
    all_chunks: list[list[tuple[str, str]]] = list(
        _iter_cell_chunks(resolution, chunk_size)
    )
    total_chunks = len(all_chunks)

    # Pre-skip cached chunks before spinning the pool.
    chunk_paths: list[Path | None] = [None] * total_chunks
    pending_indices: list[int] = []
    for idx in range(total_chunks):
        p = cache_dir / f"chunk_{idx:05d}.parquet"
        if p.exists() and not fresh:
            chunk_paths[idx] = p
        else:
            pending_indices.append(idx)

    log.info(
        "gee.zonal.start",
        export_name=export_name,
        resolution=resolution,
        scale_m=scale_m,
        chunk_size=chunk_size,
        max_workers=max_workers,
        total_chunks=total_chunks,
        chunks_cached=total_chunks - len(pending_indices),
        chunks_pending=len(pending_indices),
        cache_dir=str(cache_dir),
    )

    if not pending_indices:
        log.info("gee.zonal.all_cached", export_name=export_name)
    else:
        target_image = image.select(band_names) if band_names else image

        def _process_chunk(idx: int) -> tuple[int, Path, float, int]:
            cells_chunk = all_chunks[idx]
            h3_ids = [h for h, _ in cells_chunk]
            features = [
                ee.Feature(_wkt_polygon_to_ee_geometry(wkt), {"h3_id": h3_id})
                for h3_id, wkt in cells_chunk
            ]
            fc = ee.FeatureCollection(features)

            reduced = target_image.reduceRegions(
                collection=fc,
                reducer=reducer,
                scale=scale_m,
                crs=crs,
                tileScale=tile_scale,
            )
            reduced = reduced.map(lambda f: f.setGeometry(None))

            started = time.monotonic()
            info = reduced.getInfo()
            elapsed = time.monotonic() - started

            df = _properties_to_dataframe(info, h3_ids)
            path = cache_dir / f"chunk_{idx:05d}.parquet"
            df.to_parquet(path, index=False)
            return idx, path, elapsed, len(df)

        # Log progress every N completions (or every chunk for tiny jobs).
        pending = len(pending_indices)
        log_every = max(1, pending // 20)

        run_started = time.monotonic()
        completed = 0
        total_compute_seconds = 0.0

        if max_workers <= 1:
            # Serial path — used by tests for deterministic ordering.
            for idx in pending_indices:
                r_idx, r_path, r_elapsed, r_rows = _process_chunk(idx)
                chunk_paths[r_idx] = r_path
                completed += 1
                total_compute_seconds += r_elapsed
                if completed % log_every == 0 or completed == pending:
                    _log_progress(
                        export_name, completed, pending,
                        r_idx, r_rows, r_elapsed,
                        run_started, total_compute_seconds, max_workers,
                    )
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(_process_chunk, idx): idx for idx in pending_indices}
                for future in as_completed(futures):
                    r_idx, r_path, r_elapsed, r_rows = future.result()
                    chunk_paths[r_idx] = r_path
                    completed += 1
                    total_compute_seconds += r_elapsed
                    if completed % log_every == 0 or completed == pending:
                        _log_progress(
                            export_name, completed, pending,
                            r_idx, r_rows, r_elapsed,
                            run_started, total_compute_seconds, max_workers,
                        )

    if not any(chunk_paths):
        log.warning("gee.zonal.empty", export_name=export_name)
        return pd.DataFrame()

    dfs = [pd.read_parquet(p) for p in chunk_paths if p is not None]
    out = pd.concat(dfs, ignore_index=True)
    log.info(
        "gee.zonal.complete",
        export_name=export_name,
        chunks=len(dfs),
        rows=len(out),
        columns=list(out.columns),
    )
    return out


def _log_progress(
    export_name: str,
    completed: int,
    pending: int,
    last_chunk_idx: int,
    last_rows: int,
    last_seconds: float,
    run_started: float,
    total_compute_seconds: float,
    max_workers: int,
) -> None:
    wall_elapsed = time.monotonic() - run_started
    # Effective throughput accounts for parallelism: total compute time / workers
    # approximates the wall time we'd see if utilisation were perfect.
    avg_compute = total_compute_seconds / completed
    eta_seconds = (avg_compute / max(max_workers, 1)) * (pending - completed)
    log.info(
        "gee.zonal.progress",
        export_name=export_name,
        done=completed,
        pending=pending,
        pct=round(100.0 * completed / pending, 1),
        last_chunk=last_chunk_idx,
        last_rows=last_rows,
        last_seconds=round(last_seconds, 2),
        avg_compute_s=round(avg_compute, 2),
        wall_s=round(wall_elapsed, 1),
        eta=_format_eta(eta_seconds),
    )

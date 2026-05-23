"""Chunked GEE zonal stats with **streaming chunk consumption** to bound memory.

Each layer's pipeline:

  1. Cells stream from Postgres (``h3_cells_res{R}``) in chunks of
     ``chunk_size``. Chunks are produced **lazily** from the DB cursor —
     we never materialise the full 600k-cell list in RAM.
  2. Already-cached chunks are skipped before any worker spins up.
  3. Pending chunks run **in parallel** via ``ThreadPoolExecutor``
     (default ``max_workers=4`` — laptop-safe; GEE reduceRegions().getInfo()
     is a blocking HTTP call that releases the GIL).
  4. Each chunk's result is written to
     ``data/interim/gee/{export_name}/res{R}/chunk_NNNNN.parquet`` —
     resumable: a re-run with the same chunk_size skips finished work.
  5. As soon as a chunk lands on disk, the optional ``on_chunk`` callback
     is invoked with the freshly-read DataFrame. The chunk is then
     discarded — at no point do we hold more than ``max_workers`` chunk
     payloads in memory simultaneously.

The previous version concatenated every chunk DataFrame at the end (~600k
rows × 6 layers ≈ multi-GB peak) and crashed laptops. The new contract
returns just a row count; the callback owns persistence.

Why threads, not asyncio: ``earthengine-api`` is built on synchronous
googleapiclient/httplib2; there is no native async EE client. For our
workload (network-I/O-bound, waiting on GEE compute), threads and
asyncio are throughput-equivalent.

Why not multiprocessing: each subprocess would need its own ee.Initialize
(~1.5s) and IPC of chunk results would dominate. Threads share `ee` state
for free.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import ee
import pandas as pd
from sqlalchemy import text

from app.core.config import PROJECT_ROOT, load_pipeline_config
from app.core.db import session_scope
from app.core.logging import get_logger
from app.ingest.gee.client import init_ee

log = get_logger("ingest.gee.zonal_export")

ChunkConsumer = Callable[[pd.DataFrame], None]


def _wkt_polygon_to_ee_geometry(wkt: str) -> ee.Geometry:
    """Parse a ``POLYGON((x1 y1, x2 y2, …))`` WKT into ``ee.Geometry.Polygon``.

    Lightweight: enough for H3 hex boundaries which are always single-ring
    polygons. For general WKT use shapely.wkt.loads + json.dumps.
    """
    inner = wkt[wkt.index("((") + 2 : wkt.rindex("))")]
    ring = [tuple(map(float, p.strip().split())) for p in inner.split(",")]
    return ee.Geometry.Polygon([ring], proj=None, geodesic=False)


def _iter_cell_chunks(resolution: int, chunk_size: int) -> Iterator[list[tuple[str, str]]]:
    """Stream lists of (h3_id, wkt) tuples from Postgres lazily.

    Uses a server-side cursor (yield_per) so the entire cell table never
    sits in Python memory at once.
    """
    sql = text(
        f"""
        SELECT h3_id::text AS h3_id, ST_AsText(geom) AS wkt
        FROM dc_india.h3_cells_res{resolution}
        ORDER BY h3_id
        """
    )
    with session_scope() as session:
        result = session.execute(sql).yield_per(chunk_size)
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


def _scan_pending(
    cache_dir: Path,
    resolution: int,
    chunk_size: int,
    fresh: bool,
) -> tuple[dict[int, list[tuple[str, str]]], list[Path]]:
    """Walk the Postgres cell stream, return the pending chunks + cached paths.

    Cached chunks are recorded by their on-disk path so the caller can
    stream them through ``on_chunk`` without re-fetching.
    """
    pending: dict[int, list[tuple[str, str]]] = {}
    cached: list[Path] = []
    for idx, cells in enumerate(_iter_cell_chunks(resolution, chunk_size)):
        p = cache_dir / f"chunk_{idx:05d}.parquet"
        if p.exists() and not fresh:
            cached.append(p)
        else:
            pending[idx] = cells
    return pending, cached


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
    on_chunk: ChunkConsumer | None = None,
) -> int:
    """Run a chunked, parallel reduceRegions over the Postgres H3 grid.

    Streams chunk DataFrames to ``on_chunk`` (if provided) as soon as
    each chunk lands on disk, keeping peak memory bounded by
    ``max_workers × chunk_size``. Returns the total number of rows
    processed (sum of all chunk row counts).

    Parameters
    ----------
    image, reducer, band_names, resolution, export_name, scale_m, crs
        See module docstring.
    chunk_size, tile_scale, max_workers
        Pulled from ``configs/pipeline.yml::gee.export`` if not given.
    fresh
        If True, ignore existing chunk Parquets and re-fetch everything.
    on_chunk
        Callable invoked for each chunk DataFrame as it becomes ready.
        Cached chunks are also passed through, so a consumer can rely on
        seeing every chunk exactly once.
    """
    init_ee()
    cfg = load_pipeline_config()
    export_cfg = cfg["gee"].get("export", {})
    chunk_size = chunk_size or int(export_cfg.get("chunk_size", 5000))
    tile_scale = tile_scale or int(export_cfg.get("tile_scale", 4))
    max_workers = max_workers or int(export_cfg.get("max_workers", 4))

    cache_dir = _cache_dir_for(export_name, resolution)

    pending, cached_paths = _scan_pending(cache_dir, resolution, chunk_size, fresh)
    total_chunks = len(pending) + len(cached_paths)

    log.info(
        "gee.zonal.start",
        export_name=export_name,
        resolution=resolution,
        scale_m=scale_m,
        chunk_size=chunk_size,
        max_workers=max_workers,
        total_chunks=total_chunks,
        chunks_cached=len(cached_paths),
        chunks_pending=len(pending),
        cache_dir=str(cache_dir),
    )

    total_rows = 0

    # Drain cached chunks first — they're already on disk and consume no
    # GEE quota. Reading one at a time keeps peak memory ~50 MB even at
    # res-7 / chunk_size 5000.
    for p in cached_paths:
        df = pd.read_parquet(p)
        total_rows += len(df)
        if on_chunk is not None:
            on_chunk(df)
        del df

    if not pending:
        if total_chunks == 0:
            log.warning("gee.zonal.empty", export_name=export_name)
        else:
            log.info("gee.zonal.all_cached", export_name=export_name)
        return total_rows

    target_image = image.select(band_names) if band_names else image

    def _process_chunk(idx: int, cells_chunk: list[tuple[str, str]]) -> tuple[int, Path, float, int]:
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

    n_pending = len(pending)
    log_every = max(1, n_pending // 20)
    run_started = time.monotonic()
    completed = 0
    total_compute_seconds = 0.0

    pending_items = list(pending.items())

    if max_workers <= 1:
        # Serial path — deterministic ordering for tests.
        for idx, cells_chunk in pending_items:
            r_idx, r_path, r_elapsed, r_rows = _process_chunk(idx, cells_chunk)
            completed += 1
            total_compute_seconds += r_elapsed
            total_rows += r_rows
            if on_chunk is not None:
                df = pd.read_parquet(r_path)
                on_chunk(df)
                del df
            if completed % log_every == 0 or completed == n_pending:
                _log_progress(
                    export_name, completed, n_pending, r_idx, r_rows,
                    r_elapsed, run_started, total_compute_seconds, max_workers,
                )
    else:
        # Bounded-pipeline scheduler: keep at most `max_workers` in flight.
        # We never queue all chunks at once (this avoided multi-GB future
        # backlog at res-7).
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            it = iter(pending_items)
            in_flight = {}
            for _ in range(max_workers):
                try:
                    idx, cells_chunk = next(it)
                except StopIteration:
                    break
                in_flight[pool.submit(_process_chunk, idx, cells_chunk)] = idx

            while in_flight:
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for fut in done:
                    r_idx, r_path, r_elapsed, r_rows = fut.result()
                    in_flight.pop(fut)
                    completed += 1
                    total_compute_seconds += r_elapsed
                    total_rows += r_rows
                    if on_chunk is not None:
                        df = pd.read_parquet(r_path)
                        on_chunk(df)
                        del df
                    if completed % log_every == 0 or completed == n_pending:
                        _log_progress(
                            export_name, completed, n_pending, r_idx, r_rows,
                            r_elapsed, run_started, total_compute_seconds, max_workers,
                        )
                    try:
                        idx, cells_chunk = next(it)
                    except StopIteration:
                        continue
                    in_flight[pool.submit(_process_chunk, idx, cells_chunk)] = idx

    if total_rows == 0:
        log.warning("gee.zonal.empty", export_name=export_name)
        return 0

    log.info(
        "gee.zonal.complete",
        export_name=export_name,
        chunks=total_chunks,
        rows=total_rows,
    )
    return total_rows


def collect_zonal_export(**kwargs) -> pd.DataFrame:
    """Backward-compat helper that concatenates chunks into a single DataFrame.

    Only meant for small ad-hoc usage / tests — bulk pipelines should
    instead pass ``on_chunk`` to ``run_zonal_export`` so chunks are
    streamed straight to Postgres without ever co-existing in memory.
    """
    frames: list[pd.DataFrame] = []
    run_zonal_export(on_chunk=frames.append, **kwargs)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


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
    avg_compute = total_compute_seconds / max(completed, 1)
    eta_seconds = (avg_compute / max(max_workers, 1)) * (pending - completed)
    log.info(
        "gee.zonal.progress",
        export_name=export_name,
        done=completed,
        pending=pending,
        pct=round(100.0 * completed / max(pending, 1), 1),
        last_chunk=last_chunk_idx,
        last_rows=last_rows,
        last_seconds=round(last_seconds, 2),
        avg_compute_s=round(avg_compute, 2),
        wall_s=round(wall_elapsed, 1),
        eta=_format_eta(eta_seconds),
    )

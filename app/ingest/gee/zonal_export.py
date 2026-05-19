"""Chunked synchronous GEE zonal stats; per-chunk Parquet cache on disk.

Replaces the previous "publish-asset → Export.table.toCloudStorage → read
back from GCS" pattern with a simpler local-disk pipeline:

  1. Cells stream from Postgres (``h3_cells_res{R}``) in chunks of
     ``chunk_size`` (default 2000).
  2. Each chunk becomes an inline ``ee.FeatureCollection`` (~2 MB, well
     under the 10 MB request cap).
  3. ``reduceRegions(...).getInfo()`` runs synchronously.
  4. The chunk's result is written to
     ``data/interim/gee/{export_name}/res{R}/chunk_NNNNN.parquet`` —
     fully resumable: a re-run skips chunks whose Parquet already exists.
  5. The driver returns the concatenated DataFrame.

Trade-off vs. GCS-async-export:
  + No GCS bucket / service-account JSON required.
  + No async polling; straight-line Python execution.
  + Resumable on transient GEE errors (per-chunk granularity).
  + Single source of truth for cells (Postgres). No upfront asset
    publication needed; ``make push-grid-to-gee`` becomes optional.
  - Sequential, so somewhat slower in wall time than parallel batch
    exports. At ~2000 cells/chunk × 5–30 s/chunk for 644k res-7 cells
    that's 10–60 min per raster layer, comparable to the old pattern in
    practice once GEE task queuing is accounted for.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
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
    fresh: bool = False,
) -> pd.DataFrame:
    """Chunked synchronous reduceRegions over the Postgres-resident H3 grid.

    Parameters
    ----------
    image
        ``ee.Image`` to reduce.
    reducer
        ``ee.Reducer`` to apply (e.g. ``ee.Reducer.mean()``,
        ``ee.Reducer.mean().combine(ee.Reducer.max(), sharedInputs=True)``).
    band_names
        Bands to keep on ``image`` before reducing. Pass an empty list to
        use every band the image already exposes (useful when the raster
        is user-uploaded and the band name isn't known a priori).
    resolution
        H3 resolution; cells are read from ``h3_cells_res{R}``.
    export_name
        Friendly name used in the cache path (``data/interim/gee/<name>/...``).
    scale_m
        Reducer scale in metres; match the raster's native pixel size.
    chunk_size
        Cells per GEE request. Default from ``configs/pipeline.yml::gee.export.chunk_size``.
    tile_scale
        Raise to 8 or 16 on "Computed value too large" errors.
    fresh
        If True, ignore existing chunk Parquets and re-fetch everything.

    Returns
    -------
    Concatenated DataFrame with ``h3_id`` + reducer-output columns.
    """
    init_ee()
    cfg = load_pipeline_config()
    export_cfg = cfg["gee"].get("export", {})
    chunk_size = chunk_size or int(export_cfg.get("chunk_size", 2000))
    tile_scale = tile_scale or int(export_cfg.get("tile_scale", 4))

    cache_dir = _cache_dir_for(export_name, resolution)
    log.info(
        "gee.zonal.start",
        export_name=export_name,
        resolution=resolution,
        scale_m=scale_m,
        chunk_size=chunk_size,
        cache_dir=str(cache_dir),
    )

    target_image = image.select(band_names) if band_names else image
    chunk_paths: list[Path] = []
    chunk_idx = 0
    total_rows = 0

    for cells_chunk in _iter_cell_chunks(resolution, chunk_size):
        chunk_path = cache_dir / f"chunk_{chunk_idx:05d}.parquet"
        chunk_paths.append(chunk_path)

        if chunk_path.exists() and not fresh:
            log.debug(
                "gee.zonal.chunk.cached",
                chunk_idx=chunk_idx,
                rows=len(cells_chunk),
            )
            chunk_idx += 1
            total_rows += len(cells_chunk)
            continue

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
        # Drop geometry from the response to shrink the payload.
        reduced = reduced.map(lambda f: f.setGeometry(None))

        started = time.monotonic()
        info = reduced.getInfo()
        elapsed = time.monotonic() - started

        df = _properties_to_dataframe(info, h3_ids)
        df.to_parquet(chunk_path, index=False)

        log.info(
            "gee.zonal.chunk.done",
            chunk_idx=chunk_idx,
            rows=len(df),
            seconds=round(elapsed, 2),
        )
        chunk_idx += 1
        total_rows += len(df)

    if not chunk_paths:
        log.warning("gee.zonal.empty", export_name=export_name)
        return pd.DataFrame()

    dfs = [pd.read_parquet(p) for p in chunk_paths]
    out = pd.concat(dfs, ignore_index=True)
    log.info(
        "gee.zonal.complete",
        export_name=export_name,
        chunks=len(chunk_paths),
        rows=len(out),
        columns=list(out.columns),
    )
    return out

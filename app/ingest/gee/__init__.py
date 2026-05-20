"""GEE ingestion adapters. Dispatch by layer name."""
from __future__ import annotations

# Canonical ordering for `ingest_all` — coarsest first so any early failure
# surfaces before the most expensive layers (landcover, slope).
ALL_LAYERS: tuple[str, ...] = (
    "seismic",
    "flood",
    "slope",
    "landcover",
    "solar",
    "climate",
    "population",
)


def dispatch(layer: str, resolution: int = 7, *, fresh: bool = False) -> int:
    """Route to the correct GEE ingest module.

    ``fresh=True`` bypasses the per-source skip-if-recent guard.
    """
    from app.ingest.gee.client import init_ee

    init_ee()
    if layer == "seismic":
        from app.ingest.gee.seismic import ingest

        return ingest(resolution=resolution, fresh=fresh)
    if layer == "flood":
        from app.ingest.gee.flood import ingest

        return ingest(resolution=resolution, fresh=fresh)
    if layer == "slope":
        from app.ingest.gee.slope import ingest

        return ingest(resolution=resolution, fresh=fresh)
    if layer == "landcover":
        from app.ingest.gee.landcover import ingest

        return ingest(resolution=resolution, fresh=fresh)
    if layer == "solar":
        from app.ingest.gee.solar import ingest

        return ingest(resolution=resolution, fresh=fresh)
    if layer == "climate":
        from app.ingest.gee.climate import ingest

        return ingest(resolution=resolution, fresh=fresh)
    if layer == "population":
        from app.ingest.gee.population import ingest

        return ingest(resolution=resolution, fresh=fresh)
    raise ValueError(f"Unknown GEE layer: {layer}")


def ingest_all(
    resolution: int = 7,
    *,
    fresh: bool = False,
    parallel_layers: int | None = None,
) -> dict[str, int]:
    """Run every GEE layer in a single Python process.

    Saves ~5s × 7 layers of subprocess startup overhead vs. invoking the
    CLI per-layer from Make. Per-source skip-if-recent + per-chunk Parquet
    caching still apply, so re-runs remain idempotent.

    Concurrency model
    -----------------
    - ``parallel_layers=1`` (default): layers run one after another, each
      with its own internal chunk-parallel ThreadPoolExecutor
      (``configs/pipeline.yml::gee.export.max_workers``, default 10).
    - ``parallel_layers=N>1``: N layers run concurrently. Each still uses
      its own ``max_workers`` chunk pool, so total in-flight HTTP calls
      can reach ``N × max_workers``. Free-tier GEE typically tolerates
      ~20-30 concurrent requests; raise cautiously.

    Returns
    -------
    Mapping of layer → row count. ``0`` means skipped or empty; ``-1``
    means the layer raised (errors are aggregated into a single
    ``RuntimeError`` at the end so failure reports are complete).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from app.core.config import load_pipeline_config
    from app.core.logging import get_logger
    from app.ingest.gee.client import init_ee

    log = get_logger("ingest.gee.all")
    init_ee()

    if parallel_layers is None:
        parallel_layers = int(
            load_pipeline_config()["gee"].get("export", {}).get("layer_parallelism", 1)
        )
    parallel_layers = max(1, parallel_layers)

    results: dict[str, int] = {}
    failed: dict[str, str] = {}

    def _run_one(layer: str) -> tuple[str, int | Exception]:
        try:
            return layer, dispatch(layer=layer, resolution=resolution, fresh=fresh)
        except Exception as exc:
            return layer, exc

    if parallel_layers <= 1:
        log.info("ingest.all.start", layers=list(ALL_LAYERS), mode="sequential")
        for layer in ALL_LAYERS:
            log.info("ingest.layer.start", layer=layer)
            _, outcome = _run_one(layer)
            if isinstance(outcome, Exception):
                failed[layer] = f"{type(outcome).__name__}: {outcome}"
                log.error("ingest.layer.failed", layer=layer, error=str(outcome))
                results[layer] = -1
            else:
                results[layer] = outcome
                log.info("ingest.layer.done", layer=layer, rows=outcome)
    else:
        log.info(
            "ingest.all.start",
            layers=list(ALL_LAYERS),
            mode="parallel",
            parallel_layers=parallel_layers,
        )
        with ThreadPoolExecutor(max_workers=parallel_layers) as pool:
            futures = {pool.submit(_run_one, layer): layer for layer in ALL_LAYERS}
            for future in as_completed(futures):
                layer, outcome = future.result()
                if isinstance(outcome, Exception):
                    failed[layer] = f"{type(outcome).__name__}: {outcome}"
                    log.error("ingest.layer.failed", layer=layer, error=str(outcome))
                    results[layer] = -1
                else:
                    results[layer] = outcome
                    log.info("ingest.layer.done", layer=layer, rows=outcome)

    log.info(
        "ingest.all.summary",
        succeeded=[k for k, v in results.items() if v >= 0],
        failed=list(failed),
    )
    if failed:
        msgs = "; ".join(f"{layer}: {msg}" for layer, msg in failed.items())
        raise RuntimeError(f"GEE layers failed: {msgs}")
    return results

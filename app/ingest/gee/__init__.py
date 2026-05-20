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


def ingest_all(resolution: int = 7, *, fresh: bool = False) -> dict[str, int]:
    """Run every GEE layer in a single Python process.

    Saves ~5s × 7 layers of subprocess startup overhead vs. invoking the
    CLI per-layer from Make. Per-source skip-if-recent + per-chunk Parquet
    caching still apply, so re-runs remain idempotent.

    Returns a mapping of layer → row count (or 0 if the layer was skipped).
    """
    from app.core.logging import get_logger
    from app.ingest.gee.client import init_ee

    log = get_logger("ingest.gee.all")
    init_ee()

    results: dict[str, int] = {}
    failed: dict[str, str] = {}

    for layer in ALL_LAYERS:
        log.info("ingest.layer.start", layer=layer)
        try:
            # Each layer initialises ee separately; init_ee is lru_cached so it's a no-op.
            results[layer] = dispatch(layer=layer, resolution=resolution, fresh=fresh)
            log.info("ingest.layer.done", layer=layer, rows=results[layer])
        except Exception as exc:
            failed[layer] = f"{type(exc).__name__}: {exc}"
            log.error("ingest.layer.failed", layer=layer, error=str(exc))
            # Don't abort the whole run — let later layers proceed so the
            # user gets a complete failure report in one shot.
            results[layer] = -1

    log.info(
        "ingest.all.summary",
        succeeded=[k for k, v in results.items() if v >= 0],
        failed=list(failed),
    )
    if failed:
        msgs = "; ".join(f"{layer}: {msg}" for layer, msg in failed.items())
        raise RuntimeError(f"GEE layers failed: {msgs}")
    return results

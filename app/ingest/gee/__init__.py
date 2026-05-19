"""GEE ingestion adapters. Dispatch by layer name."""
from __future__ import annotations


def dispatch(layer: str, resolution: int = 7) -> int:
    """Route to the correct GEE ingest module."""
    from app.ingest.gee.client import init_ee

    init_ee()
    if layer == "seismic":
        from app.ingest.gee.seismic import ingest

        return ingest(resolution=resolution)
    if layer == "flood":
        from app.ingest.gee.flood import ingest

        return ingest(resolution=resolution)
    if layer == "slope":
        from app.ingest.gee.slope import ingest

        return ingest(resolution=resolution)
    if layer == "landcover":
        from app.ingest.gee.landcover import ingest

        return ingest(resolution=resolution)
    if layer == "solar":
        from app.ingest.gee.solar import ingest

        return ingest(resolution=resolution)
    if layer == "climate":
        from app.ingest.gee.climate import ingest

        return ingest(resolution=resolution)
    if layer == "population":
        from app.ingest.gee.population import ingest

        return ingest(resolution=resolution)
    raise ValueError(f"Unknown GEE layer: {layer}")

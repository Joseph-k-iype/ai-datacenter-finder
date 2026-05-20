"""Static (curated) data ingestion."""
from __future__ import annotations


def dispatch(layer: str, *, fresh: bool = False) -> int:
    if layer == "cable-landings":
        from app.ingest.static.cable_landings import ingest

        return ingest(fresh=fresh)
    if layer == "metros":
        from app.ingest.static.metros import ingest

        return ingest(fresh=fresh)
    raise ValueError(f"Unknown static layer: {layer}")

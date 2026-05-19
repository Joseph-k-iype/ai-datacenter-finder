"""OSM ingestion adapters."""
from __future__ import annotations


def dispatch(layer: str, with_topology: bool = False) -> int:
    if layer == "power":
        from app.ingest.osm.power import ingest_power
        return ingest_power(with_topology=with_topology)
    if layer == "highways":
        from app.ingest.osm.highways import ingest_highways
        return ingest_highways()
    if layer == "water":
        from app.ingest.osm.water import ingest_water
        return ingest_water()
    if layer == "railways":
        from app.ingest.osm.railways import ingest_railways
        return ingest_railways()
    raise ValueError(f"Unknown OSM layer: {layer}")

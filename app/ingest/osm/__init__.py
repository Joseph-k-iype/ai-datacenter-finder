"""OSM ingestion adapters."""
from __future__ import annotations


def dispatch(layer: str, *, with_topology: bool = False, fresh: bool = False) -> int:
    if layer == "power":
        from app.ingest.osm.power import ingest_power

        return ingest_power(with_topology=with_topology, fresh=fresh)
    if layer == "highways":
        from app.ingest.osm.highways import ingest_highways

        return ingest_highways(fresh=fresh)
    if layer == "water":
        from app.ingest.osm.water import ingest_water

        return ingest_water(fresh=fresh)
    if layer == "railways":
        from app.ingest.osm.railways import ingest_railways

        return ingest_railways(fresh=fresh)
    raise ValueError(f"Unknown OSM layer: {layer}")

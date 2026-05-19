"""H3 helpers. Use h3-pg where possible; this module fills small Python-side gaps.

Important: we treat H3 cell IDs as `int` (uint64) for in-memory work and as the
native ``h3index`` type in PostGIS. When sending IDs to/from PostGIS, cast via
``h3_to_string()`` / explicit cast.
"""
from __future__ import annotations

from collections.abc import Iterable

import h3
from shapely import wkt
from shapely.geometry import MultiPolygon, Polygon


def cell_to_latlng(h3_id: int | str) -> tuple[float, float]:
    """Return (lat, lng) of cell center."""
    return h3.cell_to_latlng(h3_id)


def cell_to_polygon(h3_id: int | str) -> Polygon:
    """Return a shapely Polygon for the H3 cell boundary."""
    coords = h3.cell_to_boundary(h3_id)
    # h3 v4 returns [(lat, lng), ...]; shapely expects (x, y) = (lng, lat).
    return Polygon([(lng, lat) for lat, lng in coords])


def cells_in_geometry(geom: Polygon | MultiPolygon, resolution: int) -> set[str]:
    """Return the set of H3 cell IDs whose centers fall inside ``geom`` at ``resolution``.

    Uses h3.polygon_to_cells (v4). For MultiPolygon, iterates per part.
    """
    if isinstance(geom, MultiPolygon):
        cells: set[str] = set()
        for part in geom.geoms:
            cells |= cells_in_geometry(part, resolution)
        return cells

    exterior = [(lat, lng) for lng, lat in geom.exterior.coords]
    holes = [[(lat, lng) for lng, lat in ring.coords] for ring in geom.interiors]
    poly = h3.LatLngPoly(exterior, *holes)
    return set(h3.polygon_to_cells(poly, resolution))


def cell_area_km2(h3_id: int | str) -> float:
    return float(h3.cell_area(h3_id, unit="km^2"))


def parent(h3_id: str, parent_res: int) -> str:
    return h3.cell_to_parent(h3_id, parent_res)


def children(h3_id: str, child_res: int) -> list[str]:
    return list(h3.cell_to_children(h3_id, child_res))


def k_ring_neighbors(h3_id: str, k: int = 1) -> set[str]:
    return set(h3.grid_disk(h3_id, k))


def distance_km(a: str, b: str) -> float:
    lat1, lng1 = h3.cell_to_latlng(a)
    lat2, lng2 = h3.cell_to_latlng(b)
    return _haversine_km(lat1, lng1, lat2, lng2)


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    import math

    R = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def wkt_to_polygon(s: str) -> Polygon | MultiPolygon:
    """Parse a WKT geometry string."""
    return wkt.loads(s)


def iter_cells_to_string(cells: Iterable[int | str]) -> Iterable[str]:
    """Normalize cell IDs to string form for SQL casting."""
    for c in cells:
        yield c if isinstance(c, str) else h3.int_to_str(c)

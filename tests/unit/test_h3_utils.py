"""H3 helper tests."""
from __future__ import annotations

import h3
import pytest

from app.core.h3_utils import cell_to_polygon, cells_in_geometry, distance_km, parent


def _example_cell() -> str:
    # Near Pune.
    return h3.latlng_to_cell(18.5204, 73.8567, 7)


def test_cell_to_polygon_is_hexagon():
    poly = cell_to_polygon(_example_cell())
    assert poly.is_valid
    # Hexagon = 6 unique vertices + closing.
    assert len(poly.exterior.coords) == 7


def test_parent_relationship():
    cell = _example_cell()
    p = parent(cell, 6)
    assert h3.is_valid_cell(p)


def test_distance_zero_for_same_cell():
    cell = _example_cell()
    assert distance_km(cell, cell) == pytest.approx(0.0, abs=1e-9)


def test_cells_in_geometry_for_small_polygon():
    """A 10x10km box near Pune should contain at least ~10 res-7 cells."""
    from shapely.geometry import box

    bbox = box(73.80, 18.48, 73.90, 18.58)  # ~11x11 km
    cells = cells_in_geometry(bbox, resolution=7)
    assert 5 <= len(cells) <= 100

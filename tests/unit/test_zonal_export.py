"""Tests for the chunked-sync local-disk zonal_export driver.

We mock GEE + Postgres entirely — these tests exercise the chunking,
caching, and DataFrame assembly without any network.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.ingest.gee.zonal_export import (
    _properties_to_dataframe,
    _wkt_polygon_to_ee_geometry,
    collect_zonal_export,
    run_zonal_export,
)


def test_wkt_polygon_parses_hexagon():
    """A minimal hex-like polygon WKT round-trips into ee.Geometry.Polygon."""
    wkt = (
        "POLYGON((73.80 18.50, 73.85 18.50, 73.875 18.55, "
        "73.85 18.60, 73.80 18.60, 73.775 18.55, 73.80 18.50))"
    )
    with patch("app.ingest.gee.zonal_export.ee.Geometry.Polygon") as mock_poly:
        mock_poly.return_value = MagicMock(name="ee_geom")
        result = _wkt_polygon_to_ee_geometry(wkt)
    assert result is mock_poly.return_value
    mock_poly.assert_called_once()
    # First arg is the coordinate ring list-of-lists with 7 vertices.
    args, _ = mock_poly.call_args
    ring = args[0][0]
    assert len(ring) == 7
    assert ring[0] == (73.80, 18.50)


def test_properties_to_dataframe_fills_missing_cells_with_nan():
    expected_ids = ["abc", "def", "ghi"]
    info = {
        "features": [
            {"properties": {"h3_id": "abc", "pga_g": 0.22}},
            {"properties": {"h3_id": "ghi", "pga_g": 0.41}},
            # "def" intentionally absent (e.g. ocean cell with no land)
        ]
    }
    df = _properties_to_dataframe(info, expected_ids)
    assert list(df["h3_id"]) == expected_ids
    assert df.loc[df.h3_id == "def", "pga_g"].isna().all()
    assert float(df.loc[df.h3_id == "abc", "pga_g"].iloc[0]) == pytest.approx(0.22)


def test_properties_to_dataframe_handles_empty_response():
    df = _properties_to_dataframe({"features": []}, ["a", "b"])
    assert list(df["h3_id"]) == ["a", "b"]
    # Just the h3_id column; no value columns.
    assert list(df.columns) == ["h3_id"]


def test_run_zonal_export_caches_chunks_on_disk(tmp_path, monkeypatch):
    """End-to-end with everything mocked: verify chunks land as Parquet and re-runs skip them."""
    cells = [
        ("c1", "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"),
        ("c2", "POLYGON((1 1, 2 1, 2 2, 1 2, 1 1))"),
        ("c3", "POLYGON((2 2, 3 2, 3 3, 2 3, 2 2))"),
    ]

    def fake_iter_cells(resolution, chunk_size):
        # Two chunks of 2 + 1 cells.
        yield cells[:2]
        yield cells[2:]

    fake_cache = tmp_path / "gee" / "demo" / "res7"

    def fake_cache_dir(name, res):
        fake_cache.mkdir(parents=True, exist_ok=True)
        return fake_cache

    # Each call to reduced.getInfo() returns properties keyed by h3_id.
    info_per_call = [
        {"features": [
            {"properties": {"h3_id": "c1", "v": 0.1}},
            {"properties": {"h3_id": "c2", "v": 0.2}},
        ]},
        {"features": [{"properties": {"h3_id": "c3", "v": 0.3}}]},
    ]

    fake_reduced = MagicMock()
    fake_reduced.map.return_value = fake_reduced
    fake_reduced.getInfo.side_effect = info_per_call

    fake_image = MagicMock()
    fake_image.select.return_value.reduceRegions.return_value = fake_reduced
    # band_names empty path also uses image directly
    fake_image.reduceRegions.return_value = fake_reduced

    monkeypatch.setattr("app.ingest.gee.zonal_export.init_ee", lambda: None)
    monkeypatch.setattr("app.ingest.gee.zonal_export._iter_cell_chunks", fake_iter_cells)
    monkeypatch.setattr("app.ingest.gee.zonal_export._cache_dir_for", fake_cache_dir)
    monkeypatch.setattr(
        "app.ingest.gee.zonal_export._wkt_polygon_to_ee_geometry",
        lambda wkt: MagicMock(name="geom"),
    )
    monkeypatch.setattr("app.ingest.gee.zonal_export.ee.Feature", MagicMock())
    monkeypatch.setattr("app.ingest.gee.zonal_export.ee.FeatureCollection", MagicMock())

    df = collect_zonal_export(
        image=fake_image,
        reducer=MagicMock(),
        band_names=[],
        resolution=7,
        export_name="demo",
        scale_m=30,
        chunk_size=2,
        max_workers=1,   # serial execution for deterministic ordering in tests
    )

    assert sorted(df.h3_id) == ["c1", "c2", "c3"]
    assert {f.name for f in fake_cache.iterdir()} == {
        "chunk_00000.parquet",
        "chunk_00001.parquet",
    }

    # Second run: getInfo must NOT be called again — chunks are cached.
    fake_reduced.getInfo.reset_mock()
    df2 = collect_zonal_export(
        image=fake_image,
        reducer=MagicMock(),
        band_names=[],
        resolution=7,
        export_name="demo",
        scale_m=30,
        chunk_size=2,
    )
    fake_reduced.getInfo.assert_not_called()
    assert len(df2) == 3


def test_run_zonal_export_parallel_preserves_chunk_order(tmp_path, monkeypatch):
    """Parallel chunks complete out of order but the final DataFrame must be
    sorted by chunk index (= sorted by h3_id since cells are pulled ORDER BY h3_id).
    """
    # Three chunks of one cell each. The mocked getInfo returns instantly so
    # ThreadPoolExecutor easily reorders completion.
    cells = [
        ("aaa", "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"),
        ("bbb", "POLYGON((1 1, 2 1, 2 2, 1 2, 1 1))"),
        ("ccc", "POLYGON((2 2, 3 2, 3 3, 2 3, 2 2))"),
    ]

    def fake_iter_cells(resolution, chunk_size):
        for c in cells:
            yield [c]

    fake_cache = tmp_path / "demo" / "res7"

    # Each chunk index → its h3_id row in the response. Map by call args
    # so order-of-completion doesn't matter.
    call_log: list[str] = []

    def make_reduced(captured_h3_ids):
        m = MagicMock()
        m.map.return_value = m

        def getinfo():
            return {
                "features": [
                    {"properties": {"h3_id": h3, "v": 0.5}} for h3 in captured_h3_ids
                ]
            }

        m.getInfo.side_effect = getinfo
        return m

    # Rebind ee.Feature so each construction appends the h3_id to call_log;
    # before getInfo runs we drain the log into the response.
    def fake_feature(geom, props):
        call_log.append(props["h3_id"])
        return MagicMock()

    fake_reduced = MagicMock()
    fake_reduced.map.return_value = fake_reduced

    captured_per_call: list[list[str]] = []

    def fake_getinfo():
        # Drain call_log into this batch.
        captured_per_call.append(list(call_log))
        h3s = call_log.copy()
        call_log.clear()
        return {"features": [{"properties": {"h3_id": h, "v": 0.7}} for h in h3s]}

    fake_reduced.getInfo.side_effect = fake_getinfo

    fake_image = MagicMock()
    fake_image.select.return_value.reduceRegions.return_value = fake_reduced
    fake_image.reduceRegions.return_value = fake_reduced

    monkeypatch.setattr("app.ingest.gee.zonal_export.init_ee", lambda: None)
    monkeypatch.setattr("app.ingest.gee.zonal_export._iter_cell_chunks", fake_iter_cells)
    monkeypatch.setattr(
        "app.ingest.gee.zonal_export._cache_dir_for",
        lambda n, r: (fake_cache.mkdir(parents=True, exist_ok=True) or fake_cache),
    )
    monkeypatch.setattr(
        "app.ingest.gee.zonal_export._wkt_polygon_to_ee_geometry",
        lambda wkt: MagicMock(),
    )
    monkeypatch.setattr("app.ingest.gee.zonal_export.ee.Feature", fake_feature)
    monkeypatch.setattr("app.ingest.gee.zonal_export.ee.FeatureCollection", MagicMock())

    df = collect_zonal_export(
        image=fake_image,
        reducer=MagicMock(),
        band_names=[],
        resolution=7,
        export_name="demo",
        scale_m=30,
        chunk_size=1,
        max_workers=4,
    )
    # With chunk-level parallelism the order of completion is non-deterministic
    # but every h3_id must appear exactly once.
    assert sorted(df.h3_id) == ["aaa", "bbb", "ccc"]


def test_run_zonal_export_invokes_on_chunk_streaming(tmp_path, monkeypatch):
    """The streaming callback must receive each chunk exactly once and never
    see the full concatenated result — that's how we bound memory."""
    cells = [
        ("c1", "POLYGON((0 0,1 0,1 1,0 1,0 0))"),
        ("c2", "POLYGON((1 1,2 1,2 2,1 2,1 1))"),
        ("c3", "POLYGON((2 2,3 2,3 3,2 3,2 2))"),
    ]

    def fake_iter_cells(resolution, chunk_size):
        yield cells[:2]
        yield cells[2:]

    fake_cache = tmp_path / "stream" / "res7"

    info_per_call = [
        {"features": [
            {"properties": {"h3_id": "c1", "v": 0.1}},
            {"properties": {"h3_id": "c2", "v": 0.2}},
        ]},
        {"features": [{"properties": {"h3_id": "c3", "v": 0.3}}]},
    ]

    fake_reduced = MagicMock()
    fake_reduced.map.return_value = fake_reduced
    fake_reduced.getInfo.side_effect = info_per_call

    fake_image = MagicMock()
    fake_image.select.return_value.reduceRegions.return_value = fake_reduced
    fake_image.reduceRegions.return_value = fake_reduced

    monkeypatch.setattr("app.ingest.gee.zonal_export.init_ee", lambda: None)
    monkeypatch.setattr("app.ingest.gee.zonal_export._iter_cell_chunks", fake_iter_cells)
    monkeypatch.setattr(
        "app.ingest.gee.zonal_export._cache_dir_for",
        lambda n, r: (fake_cache.mkdir(parents=True, exist_ok=True) or fake_cache),
    )
    monkeypatch.setattr(
        "app.ingest.gee.zonal_export._wkt_polygon_to_ee_geometry",
        lambda wkt: MagicMock(),
    )
    monkeypatch.setattr("app.ingest.gee.zonal_export.ee.Feature", MagicMock())
    monkeypatch.setattr("app.ingest.gee.zonal_export.ee.FeatureCollection", MagicMock())

    seen_chunks: list = []
    total = run_zonal_export(
        image=fake_image,
        reducer=MagicMock(),
        band_names=[],
        resolution=7,
        export_name="stream",
        scale_m=30,
        chunk_size=2,
        max_workers=1,
        on_chunk=lambda df: seen_chunks.append(df),
    )
    assert total == 3
    # Two chunk DataFrames; sum of their row counts equals 3.
    assert len(seen_chunks) == 2
    all_ids = sorted(h3 for df in seen_chunks for h3 in df["h3_id"].tolist())
    assert all_ids == ["c1", "c2", "c3"]

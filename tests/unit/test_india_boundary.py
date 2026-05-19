"""India boundary normalization tests."""
from __future__ import annotations

import json

from shapely.geometry import box, mapping

from app.grid import india_boundary


def test_load_states_gdf_maps_compact_names_and_dissolves_duplicates(tmp_path, monkeypatch):
    path = tmp_path / "gadm_sample.json"
    features = [
        {
            "type": "Feature",
            "properties": {"NAME_1": "AndamanandNicobar"},
            "geometry": mapping(box(92.0, 11.0, 93.0, 12.0)),
        },
        {
            "type": "Feature",
            "properties": {"NAME_1": "AndhraPradesh"},
            "geometry": mapping(box(79.0, 14.0, 80.0, 15.0)),
        },
        {
            "type": "Feature",
            "properties": {"NAME_1": "ArunachalPradesh"},
            "geometry": mapping(box(94.0, 27.0, 95.0, 28.0)),
        },
        {
            "type": "Feature",
            "properties": {"NAME_1": "ArunachalPradesh"},
            "geometry": mapping(box(95.0, 27.0, 96.0, 28.0)),
        },
        {
            "type": "Feature",
            "properties": {"NAME_1": "DadraandNagarHaveli"},
            "geometry": mapping(box(73.0, 20.0, 74.0, 21.0)),
        },
        {
            "type": "Feature",
            "properties": {"NAME_1": "DamanandDiu"},
            "geometry": mapping(box(72.0, 20.0, 73.0, 21.0)),
        },
    ]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    monkeypatch.setattr(india_boundary, "_download_gadm", lambda: path)

    gdf = india_boundary.load_states_gdf()

    assert set(gdf.state_code) == {"AN", "AP", "AR", "DH"}
    assert not gdf.state_code.duplicated().any()
    assert gdf.loc[gdf.state_code == "AP", "state_name"].item() == "Andhra Pradesh"
    assert gdf.loc[gdf.state_code == "DH", "state_name"].item() == (
        "Dadra and Nagar Haveli and Daman and Diu"
    )

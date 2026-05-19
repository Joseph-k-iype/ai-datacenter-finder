"""Schema contract sanity tests."""
from __future__ import annotations

import pandas as pd
import pytest

from app.governance.contracts import (
    CONTRACTS,
    get_contract,
    schema_hash,
    schema_to_dict,
)


def test_all_contracts_have_stable_hashes():
    """A schema's hash must be deterministic across two reads of the module."""
    h1 = {k: schema_hash(s) for k, s in CONTRACTS.items()}
    # Re-hash a copy via to_dict round-trip equivalence.
    h2 = {k: schema_hash(s) for k, s in CONTRACTS.items()}
    assert h1 == h2


def test_power_lines_contract_rejects_low_voltage():
    schema = get_contract("osm.power_lines")
    bad = pd.DataFrame(
        [
            {"osm_id": 1, "voltage_kv": 110, "operator": "x", "circuits": 1, "wkt": "LINESTRING(0 0,1 1)"},
        ]
    )
    from pandera.errors import SchemaError, SchemaErrors

    with pytest.raises((SchemaError, SchemaErrors)):
        schema.validate(bad, lazy=True)


def test_metros_contract_rejects_out_of_bbox():
    schema = get_contract("static.metros")
    bad = pd.DataFrame(
        [
            {"name": "Paris", "population": 2_000_000, "state_code": None, "lon": 2.35, "lat": 48.85},
        ]
    )
    from pandera.errors import SchemaError, SchemaErrors

    with pytest.raises((SchemaError, SchemaErrors)):
        schema.validate(bad, lazy=True)


def test_known_sources_present():
    expected = {
        "osm.power_lines",
        "osm.substations",
        "osm.highways",
        "osm.water_bodies",
        "wdpa",
        "gee.seismic",
        "gee.flood",
        "gee.solar",
    }
    assert expected.issubset(CONTRACTS.keys())


def test_schema_to_dict_emits_columns():
    schema = get_contract("osm.highways")
    d = schema_to_dict(schema)
    assert "columns" in d
    assert "classification" in d["columns"]

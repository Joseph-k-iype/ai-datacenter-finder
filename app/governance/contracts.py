"""Pandera schema contracts per source. Hashed and stored in schema_contracts table.

Adding a new source: define a `pandera.DataFrameSchema`, register it in
``CONTRACTS`` below, and call ``ensure_registered(source)`` at the start of
the ingest function. The hash automatically detects upstream-tag drift.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import pandera.pandas as pa
from pandera.pandas import Check, Column, DataFrameSchema

# ----------------------------------------------------------------------------
# OSM contracts
# ----------------------------------------------------------------------------

power_lines_schema = DataFrameSchema(
    {
        "osm_id": Column(pa.Int64, nullable=True),
        "voltage_kv": Column(pa.Int, Check.ge(220), nullable=False),
        "operator": Column(pa.String, nullable=True),
        "circuits": Column(pa.Int, nullable=True),
        "wkt": Column(pa.String, nullable=False),
    },
    strict=False,
    coerce=True,
)

substations_schema = DataFrameSchema(
    {
        "osm_id": Column(pa.Int64, nullable=True),
        "name": Column(pa.String, nullable=True),
        "voltage_kv": Column(pa.Int, Check.ge(66), nullable=True),
        "operator": Column(pa.String, nullable=True),
        "wkt": Column(pa.String, nullable=False),
    },
    strict=False,
    coerce=True,
)

highways_schema = DataFrameSchema(
    {
        "osm_id": Column(pa.Int64, nullable=True),
        "ref": Column(pa.String, nullable=True),
        "classification": Column(pa.String, Check.isin(["motorway", "trunk"]), nullable=False),
        "wkt": Column(pa.String, nullable=False),
    },
    strict=False,
    coerce=True,
)

railways_schema = DataFrameSchema(
    {
        "osm_id": Column(pa.Int64, nullable=True),
        "wkt": Column(pa.String, nullable=False),
    },
    strict=False,
    coerce=True,
)

water_bodies_schema = DataFrameSchema(
    {
        "osm_id": Column(pa.Int64, nullable=True),
        "name": Column(pa.String, nullable=True),
        "kind": Column(pa.String, Check.isin(["river", "lake", "reservoir", "pond"]), nullable=False),
        "wkt": Column(pa.String, nullable=False),
    },
    strict=False,
    coerce=True,
)

# ----------------------------------------------------------------------------
# WDPA contract
# ----------------------------------------------------------------------------
wdpa_schema = DataFrameSchema(
    {
        "wdpa_id": Column(pa.Int64, nullable=True),
        "name": Column(pa.String, nullable=True),
        "designation": Column(pa.String, nullable=True),
        "iucn_cat": Column(pa.String, nullable=True),
        "wkt": Column(pa.String, nullable=False),
    },
    strict=False,
    coerce=True,
)

# ----------------------------------------------------------------------------
# Stakeholder layers — populated by Overpass, not curated CSVs
# ----------------------------------------------------------------------------
sez_schema = DataFrameSchema(
    {
        "osm_id": Column(pa.Int64, nullable=True),
        "name": Column(pa.String, nullable=True),
        "policy_tag": Column(pa.String, nullable=True),
        "wkt": Column(pa.String, nullable=False),
    },
    strict=False,
    coerce=True,
)

data_centers_schema = DataFrameSchema(
    {
        "osm_id": Column(pa.Int64, nullable=True),
        "name": Column(pa.String, nullable=True),
        "company": Column(pa.String, nullable=True),
        "tier": Column(pa.Int64, Check.in_range(1, 4), nullable=True),
        "wkt": Column(pa.String, nullable=False),
    },
    strict=False,
    coerce=True,
)


# ----------------------------------------------------------------------------
# Static lists
# ----------------------------------------------------------------------------
cable_landings_schema = DataFrameSchema(
    {
        "name": Column(pa.String, nullable=False),
        "city": Column(pa.String, nullable=True),
        "lon": Column(pa.Float, Check.in_range(68.0, 97.5)),
        "lat": Column(pa.Float, Check.in_range(6.5, 37.5)),
    },
    strict=False,
    coerce=True,
)

metros_schema = DataFrameSchema(
    {
        "name": Column(pa.String, nullable=False),
        "population": Column(pa.Int64, Check.ge(100_000)),
        "state_code": Column(pa.String, nullable=True),
        "lon": Column(pa.Float, Check.in_range(68.0, 97.5)),
        "lat": Column(pa.Float, Check.in_range(6.5, 37.5)),
    },
    strict=False,
    coerce=True,
)

# ----------------------------------------------------------------------------
# GEE zonal-stat contracts (generic; per-layer specifics in the layer module)
# ----------------------------------------------------------------------------
zonal_seismic_schema = DataFrameSchema(
    {
        "h3_id": Column(pa.String),
        "pga_g": Column(pa.Float, Check.in_range(0.0, 2.0), nullable=True),
    },
    strict=False, coerce=True,
)

zonal_flood_schema = DataFrameSchema(
    {
        "h3_id": Column(pa.String),
        "occurrence_pct": Column(pa.Float, Check.in_range(0.0, 100.0), nullable=True),
        "seasonality": Column(pa.Float, nullable=True),
    },
    strict=False, coerce=True,
)

zonal_slope_schema = DataFrameSchema(
    {
        "h3_id": Column(pa.String),
        "mean_slope_deg": Column(pa.Float, Check.in_range(0.0, 90.0), nullable=True),
        "max_slope_deg": Column(pa.Float, Check.in_range(0.0, 90.0), nullable=True),
        "p95_slope_deg": Column(pa.Float, Check.in_range(0.0, 90.0), nullable=True),
    },
    strict=False, coerce=True,
)

zonal_landcover_schema = DataFrameSchema(
    {
        "h3_id": Column(pa.String),
        "forest_pct": Column(pa.Float, Check.in_range(0.0, 100.0), nullable=True),
        "cropland_pct": Column(pa.Float, Check.in_range(0.0, 100.0), nullable=True),
        "urban_pct": Column(pa.Float, Check.in_range(0.0, 100.0), nullable=True),
        "water_pct": Column(pa.Float, Check.in_range(0.0, 100.0), nullable=True),
        "bareland_pct": Column(pa.Float, Check.in_range(0.0, 100.0), nullable=True),
        "wetland_pct": Column(pa.Float, Check.in_range(0.0, 100.0), nullable=True),
    },
    strict=False, coerce=True,
)

zonal_solar_schema = DataFrameSchema(
    {
        "h3_id": Column(pa.String),
        "pvout_kwh_per_kwp": Column(pa.Float, Check.in_range(800.0, 2500.0), nullable=True),
        "ghi_kwh_per_m2": Column(pa.Float, nullable=True),
    },
    strict=False, coerce=True,
)

zonal_climate_schema = DataFrameSchema(
    {
        "h3_id": Column(pa.String),
        "mean_temp_c": Column(pa.Float, Check.in_range(-10.0, 50.0), nullable=True),
        "mean_rh_pct": Column(pa.Float, Check.in_range(0.0, 100.0), nullable=True),
    },
    strict=False, coerce=True,
)

zonal_population_schema = DataFrameSchema(
    {
        "h3_id": Column(pa.String),
        "pop_total": Column(pa.Float, Check.ge(0), nullable=True),
        "pop_density_per_km2": Column(pa.Float, Check.ge(0), nullable=True),
    },
    strict=False, coerce=True,
)


CONTRACTS: dict[str, DataFrameSchema] = {
    "osm.power_lines":    power_lines_schema,
    "osm.substations":    substations_schema,
    "osm.highways":       highways_schema,
    "osm.railways":       railways_schema,
    "osm.water_bodies":   water_bodies_schema,
    "osm.sez":            sez_schema,
    "osm.data_centers":   data_centers_schema,
    "wdpa":               wdpa_schema,
    "static.cable_landings": cable_landings_schema,
    "static.metros":      metros_schema,
    "gee.seismic":        zonal_seismic_schema,
    "gee.flood":          zonal_flood_schema,
    "gee.slope":          zonal_slope_schema,
    "gee.landcover":      zonal_landcover_schema,
    "gee.solar":          zonal_solar_schema,
    "gee.climate":        zonal_climate_schema,
    "gee.population":     zonal_population_schema,
}


def schema_to_dict(schema: DataFrameSchema) -> dict[str, Any]:
    """Serialize a Pandera schema to a JSON-able dict (for the schema_contracts table)."""
    cols: dict[str, Any] = {}
    for name, col in schema.columns.items():
        cols[name] = {
            "dtype": str(col.dtype),
            "nullable": col.nullable,
        }
    return {"columns": cols, "strict": schema.strict}


def schema_hash(schema: DataFrameSchema) -> str:
    payload = json.dumps(schema_to_dict(schema), sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def get_contract(source: str) -> DataFrameSchema:
    if source not in CONTRACTS:
        raise KeyError(f"No schema contract registered for source '{source}'")
    return CONTRACTS[source]

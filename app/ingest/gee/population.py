"""WorldPop 100m population sum + density per cell."""
from __future__ import annotations

import ee

from app.core.config import load_pipeline_config
from app.governance.contracts import get_contract, schema_hash
from app.governance.lineage import ingestion_run
from app.ingest.base import validate_and_split
from app.ingest.gee._common import upsert_zonal
from app.ingest.gee.zonal_export import run_zonal_export


def ingest(resolution: int = 7) -> int:
    cfg = load_pipeline_config()
    asset = cfg["gee"]["layers"]["population"]
    scale = cfg["gee"]["scale_m"]["population"]
    window = cfg["gee"]["windows"]["population"]
    cell_area_km2 = float(cfg["h3_avg_area_km2"][resolution])
    contract = get_contract("gee.population")

    with ingestion_run(
        source="gee.population",
        upstream_source=asset,
        schema_hash=schema_hash(contract),
    ) as run:
        pop = (
            ee.ImageCollection(asset)
            .filter(ee.Filter.eq("country", window["country_iso3"]))
            .filter(ee.Filter.eq("year", int(window["year"])))
            .first()
            .select("population")
            .rename("pop_total")
        )
        df = run_zonal_export(
            image=pop,
            reducer=ee.Reducer.sum().setOutputs(["pop_total"]),
            band_names=["pop_total"],
            resolution=resolution,
            export_name="pop_worldpop",
            scale_m=scale,
        )
        df["pop_total"] = df["pop_total"].astype(float).fillna(0.0)
        df["pop_density_per_km2"] = df["pop_total"] / cell_area_km2
        df = df[["h3_id", "pop_total", "pop_density_per_km2"]]

        clean, rejected = validate_and_split(
            df, contract, run_id=str(run.run_id), source="gee.population"
        )
        n = upsert_zonal(
            df=clean,
            table="raster_zonal_population",
            resolution=resolution,
            run_id=str(run.run_id),
            columns=["pop_total", "pop_density_per_km2"],
        )
        run.row_count = n
        run.rows_rejected = rejected
        return n

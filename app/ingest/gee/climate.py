"""ERA5-Land mean annual temperature + relative humidity per cell."""
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
    asset = cfg["gee"]["layers"]["climate"]
    scale = cfg["gee"]["scale_m"]["climate"]
    contract = get_contract("gee.climate")

    window = cfg["gee"]["windows"]["climate"]

    with ingestion_run(
        source="gee.climate",
        upstream_source=asset,
        schema_hash=schema_hash(contract),
    ) as run:
        # 2 m air temperature → Celsius; dewpoint → RH via Magnus formula.
        coll = (
            ee.ImageCollection(asset)
            .filterDate(window["start_date"], window["end_date"])
            .select(["temperature_2m", "dewpoint_temperature_2m"])
        )
        mean = coll.mean()
        t_c = mean.select("temperature_2m").subtract(273.15).rename("mean_temp_c")
        td_c = mean.select("dewpoint_temperature_2m").subtract(273.15)

        # Magnus formula for RH from T and Td.
        a, b = 17.625, 243.04
        es_t = t_c.multiply(a).divide(t_c.add(b)).exp()
        es_td = td_c.multiply(a).divide(td_c.add(b)).exp()
        rh = es_td.divide(es_t).multiply(100).rename("mean_rh_pct").clamp(0, 100)

        combined = t_c.addBands(rh)
        df = run_zonal_export(
            image=combined,
            reducer=ee.Reducer.mean(),
            band_names=["mean_temp_c", "mean_rh_pct"],
            resolution=resolution,
            export_name="climate_era5",
            scale_m=scale,
        )
        df = df[["h3_id", "mean_temp_c", "mean_rh_pct"]]

        clean, rejected = validate_and_split(
            df, contract, run_id=str(run.run_id), source="gee.climate"
        )
        n = upsert_zonal(
            df=clean,
            table="raster_zonal_climate",
            resolution=resolution,
            run_id=str(run.run_id),
            columns=["mean_temp_c", "mean_rh_pct"],
        )
        run.row_count = n
        run.rows_rejected = rejected
        return n

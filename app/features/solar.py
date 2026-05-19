"""Merge solar + climate + population zonal values into cell_features."""
from __future__ import annotations

from sqlalchemy import text

from app.core.db import session_scope
from app.core.logging import get_logger

log = get_logger("features.solar")


def merge_climate_solar_pop(resolution: int) -> int:
    cf = f"cell_features_res{resolution}"
    sql = text(
        f"""
        UPDATE dc_india.{cf} cf
        SET annual_pvout_kwh_per_kwp = sol.pvout_kwh_per_kwp,
            ghi_kwh_per_m2           = sol.ghi_kwh_per_m2,
            mean_temp_c              = cli.mean_temp_c,
            mean_rh_pct              = cli.mean_rh_pct,
            pop_density_per_km2      = pop.pop_density_per_km2
        FROM dc_india.raster_zonal_solar      sol,
             dc_india.raster_zonal_climate    cli,
             dc_india.raster_zonal_population pop
        WHERE sol.h3_id = cf.h3_id AND sol.resolution = :res
          AND cli.h3_id = cf.h3_id AND cli.resolution = :res
          AND pop.h3_id = cf.h3_id AND pop.resolution = :res
        """
    )
    with session_scope() as session:
        result = session.execute(sql, {"res": resolution})
        n = result.rowcount or 0
    log.info("features.solar_climate_pop.merged", rows=n)
    return n

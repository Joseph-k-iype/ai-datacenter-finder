"""Stage A: hard-exclusion binary mask.

Joins the raster_zonal_* tables to cell_features_res{R} and applies the
thresholds from configs/exclusions.yml. Each excluded cell carries an array
of human-readable ``exclusion_reasons`` so the UI can explain WHY it was
disqualified.

Note on coastline / CRZ-I: a proper CRZ-I exclusion requires authoritative
coastline + buffer-zone data from MoEFCC. We do not ingest it in the PoC;
``coast_buffer_km`` is left NULL and not used in the is_excluded composite.
``docs/tier4_methodology.md`` documents the gap and the upgrade path.
"""
from __future__ import annotations

from sqlalchemy import text

from app.core.config import load_exclusions
from app.core.db import session_scope
from app.core.logging import get_logger
from app.governance.lineage import ingestion_run

log = get_logger("features.exclusions")


def compute_exclusions(resolution: int) -> int:
    cfg = load_exclusions()
    flood_thr   = float(cfg["flood"]["exclude_occurrence_pct_gt"])
    slope_thr   = float(cfg["slope"]["exclude_max_slope_deg_gt"])
    urban_thr   = float(cfg["landcover"]["exclude_urban_pct_gt"])
    forest_thr  = float(cfg["landcover"]["exclude_forest_pct_gt"])
    wetland_thr = float(cfg["landcover"]["exclude_wetland_pct_gt"])
    water_thr   = float(cfg["landcover"]["exclude_water_pct_gt"])

    table = f"cell_features_res{resolution}"
    grid_table = f"h3_cells_res{resolution}"

    with ingestion_run(source=f"features.exclusions.res{resolution}") as run:
        with session_scope() as session:
            sql = text(
                f"""
                INSERT INTO dc_india.{table}
                  (h3_id, state_code,
                   in_seismic_zone_v, flood_occurrence_pct, max_slope_deg,
                   in_wdpa, urban_cover_pct, forest_cover_pct,
                   is_excluded, exclusion_reasons,
                   pipeline_run_id)
                SELECT
                    g.h3_id,
                    g.state_code,
                    COALESCE(s.in_zone_v, FALSE)            AS in_seismic_zone_v,
                    COALESCE(f.occurrence_pct, 0)           AS flood_occurrence_pct,
                    COALESCE(sl.max_slope_deg, 0)           AS max_slope_deg,
                    EXISTS (
                        SELECT 1 FROM dc_india.raw_protected_areas pa
                        WHERE ST_Intersects(pa.geom, g.geom)
                    )                                       AS in_wdpa,
                    COALESCE(lc.urban_pct, 0)               AS urban_cover_pct,
                    COALESCE(lc.forest_pct, 0)              AS forest_cover_pct,
                    (
                        COALESCE(s.in_zone_v, FALSE)
                        OR COALESCE(f.occurrence_pct, 0) > :flood_thr
                        OR COALESCE(sl.max_slope_deg, 0) > :slope_thr
                        OR COALESCE(lc.urban_pct, 0) > :urban_thr
                        OR COALESCE(lc.forest_pct, 0) > :forest_thr
                        OR COALESCE(lc.wetland_pct, 0) > :wetland_thr
                        OR COALESCE(lc.water_pct, 0) > :water_thr
                        OR EXISTS (
                            SELECT 1 FROM dc_india.raw_protected_areas pa
                            WHERE ST_Intersects(pa.geom, g.geom)
                        )
                    )                                       AS is_excluded,
                    ARRAY_REMOVE(ARRAY[
                        CASE WHEN COALESCE(s.in_zone_v, FALSE)             THEN 'seismic_zone_v'   END,
                        CASE WHEN COALESCE(f.occurrence_pct,0) > :flood_thr  THEN 'flood_recurrence' END,
                        CASE WHEN COALESCE(sl.max_slope_deg,0) > :slope_thr  THEN 'slope_too_steep'  END,
                        CASE WHEN COALESCE(lc.urban_pct,0)   > :urban_thr    THEN 'urban_built_up'   END,
                        CASE WHEN COALESCE(lc.forest_pct,0)  > :forest_thr   THEN 'forest_cover'     END,
                        CASE WHEN COALESCE(lc.wetland_pct,0) > :wetland_thr  THEN 'wetland'          END,
                        CASE WHEN COALESCE(lc.water_pct,0)   > :water_thr    THEN 'water_body'       END,
                        CASE WHEN EXISTS (
                          SELECT 1 FROM dc_india.raw_protected_areas pa
                          WHERE ST_Intersects(pa.geom, g.geom)
                        ) THEN 'protected_area' END
                    ], NULL),
                    :run_id
                FROM dc_india.{grid_table} g
                LEFT JOIN dc_india.raster_zonal_seismic   s  ON s.h3_id  = g.h3_id AND s.resolution  = :res
                LEFT JOIN dc_india.raster_zonal_flood     f  ON f.h3_id  = g.h3_id AND f.resolution  = :res
                LEFT JOIN dc_india.raster_zonal_slope     sl ON sl.h3_id = g.h3_id AND sl.resolution = :res
                LEFT JOIN dc_india.raster_zonal_landcover lc ON lc.h3_id = g.h3_id AND lc.resolution = :res
                ON CONFLICT (h3_id) DO UPDATE
                  SET in_seismic_zone_v   = EXCLUDED.in_seismic_zone_v,
                      flood_occurrence_pct = EXCLUDED.flood_occurrence_pct,
                      max_slope_deg       = EXCLUDED.max_slope_deg,
                      in_wdpa             = EXCLUDED.in_wdpa,
                      urban_cover_pct     = EXCLUDED.urban_cover_pct,
                      forest_cover_pct    = EXCLUDED.forest_cover_pct,
                      is_excluded         = EXCLUDED.is_excluded,
                      exclusion_reasons   = EXCLUDED.exclusion_reasons,
                      computed_at         = now(),
                      pipeline_run_id     = EXCLUDED.pipeline_run_id
                """
            )
            result = session.execute(
                sql,
                {
                    "res": resolution,
                    "run_id": str(run.run_id),
                    "flood_thr": flood_thr,
                    "slope_thr": slope_thr,
                    "urban_thr": urban_thr,
                    "forest_thr": forest_thr,
                    "wetland_thr": wetland_thr,
                    "water_thr": water_thr,
                },
            )
            n = result.rowcount or 0
        run.row_count = n
    log.info("features.exclusions.done", res=resolution, rows=n)
    return n

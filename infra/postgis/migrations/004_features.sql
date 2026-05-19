SET search_path TO dc_india, public;

-- ============================================================================
-- The "wide" per-cell feature table. One row per cell per resolution.
-- Populated by app/features/build.py. Treat as the read-side projection
-- the scoring engine and Streamlit cache load into memory.
-- ============================================================================
CREATE TABLE IF NOT EXISTS cell_features_res7 (
    h3_id                                h3index PRIMARY KEY,
    state_code                           TEXT NOT NULL,

    -- Exclusion flags (Stage A: hard mask)
    is_excluded                          BOOLEAN NOT NULL DEFAULT FALSE,
    exclusion_reasons                    TEXT[] NOT NULL DEFAULT '{}',
    in_seismic_zone_v                    BOOLEAN,
    flood_occurrence_pct                 REAL,
    max_slope_deg                        REAL,
    in_wdpa                              BOOLEAN,
    urban_cover_pct                      REAL,
    forest_cover_pct                     REAL,
    coast_buffer_km                      REAL,    -- distance to coastline; <0.5km = CRZ-I excluded

    -- Scoring features (Stage B inputs)
    nearest_hv_line_km                   REAL,
    nearest_hv_line_distinct_subgrid_km  REAL,   -- THE Tier-4 differentiator
    nearest_substation_km                REAL,
    nearest_water_km                     REAL,
    nearest_river_km                     REAL,
    nearest_highway_km                   REAL,
    nearest_railway_km                   REAL,
    nearest_metro_km                     REAL,
    nearest_cable_landing_km             REAL,
    annual_pvout_kwh_per_kwp             REAL,
    ghi_kwh_per_m2                       REAL,
    mean_temp_c                          REAL,
    mean_rh_pct                          REAL,
    pop_density_per_km2                  REAL,

    -- Lineage
    computed_at                          TIMESTAMPTZ NOT NULL DEFAULT now(),
    pipeline_run_id                      UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cf7_state    ON cell_features_res7 (state_code);
CREATE INDEX IF NOT EXISTS ix_cf7_excluded ON cell_features_res7 (is_excluded);

-- Mirror table for res 8 (drill-down). Same schema modulo PK name.
CREATE TABLE IF NOT EXISTS cell_features_res8 (
    h3_id                                h3index PRIMARY KEY,
    state_code                           TEXT NOT NULL,
    is_excluded                          BOOLEAN NOT NULL DEFAULT FALSE,
    exclusion_reasons                    TEXT[] NOT NULL DEFAULT '{}',
    in_seismic_zone_v                    BOOLEAN,
    flood_occurrence_pct                 REAL,
    max_slope_deg                        REAL,
    in_wdpa                              BOOLEAN,
    urban_cover_pct                      REAL,
    forest_cover_pct                     REAL,
    coast_buffer_km                      REAL,
    nearest_hv_line_km                   REAL,
    nearest_hv_line_distinct_subgrid_km  REAL,
    nearest_substation_km                REAL,
    nearest_water_km                     REAL,
    nearest_river_km                     REAL,
    nearest_highway_km                   REAL,
    nearest_railway_km                   REAL,
    nearest_metro_km                     REAL,
    nearest_cable_landing_km             REAL,
    annual_pvout_kwh_per_kwp             REAL,
    ghi_kwh_per_m2                       REAL,
    mean_temp_c                          REAL,
    mean_rh_pct                          REAL,
    pop_density_per_km2                  REAL,
    computed_at                          TIMESTAMPTZ NOT NULL DEFAULT now(),
    pipeline_run_id                      UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cf8_state ON cell_features_res8 (state_code);

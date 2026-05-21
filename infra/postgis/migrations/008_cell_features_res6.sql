SET search_path TO dc_india, public;

-- ============================================================================
-- Res-6 feature table: the coarse first sweep.
--
-- The pipeline funnels res-6 → res-7 → res-8. Res-6 is used purely for the
-- hard-exclusion pass (seismic / flood / slope / land-cover / WDPA), so it
-- only carries exclusion-related columns — no KNN distance features and no
-- raster aggregates that don't feed exclusion. Skipping those columns
-- keeps the res-6 table ~80% smaller than its res-7 sibling and the
-- coarse pass commensurately faster.
--
-- ``app/features/exclusions.py::compute_exclusions`` writes to whichever
-- ``cell_features_res{R}`` matches the call; res-6 lands here, res-7/8
-- land in the tables defined in 004_features.sql.
-- ============================================================================
CREATE TABLE IF NOT EXISTS cell_features_res6 (
    h3_id                  h3index PRIMARY KEY,
    state_code             TEXT NOT NULL,

    -- Exclusion flags (the only thing the res-6 sweep cares about)
    is_excluded            BOOLEAN NOT NULL DEFAULT FALSE,
    exclusion_reasons      TEXT[] NOT NULL DEFAULT '{}',
    in_seismic_zone_v      BOOLEAN,
    flood_occurrence_pct   REAL,
    max_slope_deg          REAL,
    in_wdpa                BOOLEAN,
    urban_cover_pct        REAL,
    forest_cover_pct       REAL,

    -- Lineage
    computed_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    pipeline_run_id        UUID NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cf6_state    ON cell_features_res6 (state_code);
CREATE INDEX IF NOT EXISTS ix_cf6_excluded ON cell_features_res6 (is_excluded);

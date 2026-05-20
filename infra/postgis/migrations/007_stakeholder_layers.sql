SET search_path TO dc_india, public;

-- ============================================================================
-- Stakeholder vector layers. Same shape as the other raw_* tables — each
-- row has an ingestion_run_id so it's poppable and the FalkorDB graph
-- projection can trace it back to a specific ingest. No CSV / hand-curated
-- data: everything here flows in from OSM (Overpass) on each refresh.
-- ============================================================================

-- Special Economic Zones (Indian DPIIT-listed and OSM-tagged industrial
-- zones). Overpass query selects:
--   * boundary=special_economic_zone
--   * landuse=industrial with name~"SEZ"
--   * place=industrial with name~"SEZ"
CREATE TABLE IF NOT EXISTS raw_sez (
    id                BIGSERIAL PRIMARY KEY,
    osm_id            BIGINT,
    name              TEXT,
    operator          TEXT,
    policy_tag        TEXT,         -- 'data_center_incentive' | 'it_services' |
                                    -- 'multi_product' | NULL (uncategorized)
    state_code        TEXT,
    geom              geometry(Geometry, 4326) NOT NULL,
    centroid          geometry(Point, 4326),
    ingestion_run_id  UUID NOT NULL,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_sez_geom     ON raw_sez USING gist (geom);
CREATE INDEX IF NOT EXISTS ix_sez_centroid ON raw_sez USING gist (centroid);
CREATE INDEX IF NOT EXISTS ix_sez_state    ON raw_sez (state_code);

-- Data centers (existing hyperscaler footprint).
-- Overpass tags:
--   * telecom=data_center
--   * office=data_center
--   * building=data_center
--   * industrial=telecommunication / data_center
-- ``tier`` mirrors Uptime Institute Tier I–IV when present in OSM tags,
-- otherwise NULL.
CREATE TABLE IF NOT EXISTS raw_data_centers (
    id                BIGSERIAL PRIMARY KEY,
    osm_id            BIGINT,
    name              TEXT,
    operator          TEXT,
    company           TEXT,
    tier              INT,
    city              TEXT,
    state_code        TEXT,
    geom              geometry(Point, 4326) NOT NULL,
    ingestion_run_id  UUID NOT NULL,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_data_centers_geom    ON raw_data_centers USING gist (geom);
CREATE INDEX IF NOT EXISTS ix_data_centers_company ON raw_data_centers (company);
CREATE INDEX IF NOT EXISTS ix_data_centers_state   ON raw_data_centers (state_code);

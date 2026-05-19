SET search_path TO dc_india, public;

-- ============================================================================
-- Raw vector ingestion tables. Each row carries ingestion_run_id so a bad
-- pull can be surgically rolled back via DELETE WHERE ingestion_run_id = ...
-- ============================================================================

-- High-voltage transmission (OSM power=line, voltage>=220kV)
CREATE TABLE IF NOT EXISTS raw_power_lines (
    id                BIGSERIAL PRIMARY KEY,
    osm_id            BIGINT,
    voltage_kv        INT,
    operator          TEXT,
    circuits          INT,
    subgrid_component INT,            -- assigned by ingest/osm/power_topology
    cluster_id        INT,            -- ST_ClusterDBSCAN: parallel-circuit cluster
    geom              geometry(LineString, 4326) NOT NULL,
    ingestion_run_id  UUID NOT NULL,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_power_lines_geom    ON raw_power_lines USING gist (geom);
CREATE INDEX IF NOT EXISTS ix_power_lines_voltage ON raw_power_lines (voltage_kv);
CREATE INDEX IF NOT EXISTS ix_power_lines_subgrid ON raw_power_lines (subgrid_component);

-- Substations
CREATE TABLE IF NOT EXISTS raw_substations (
    id                BIGSERIAL PRIMARY KEY,
    osm_id            BIGINT,
    name              TEXT,
    voltage_kv        INT,
    operator          TEXT,
    geom              geometry(Point, 4326) NOT NULL,
    ingestion_run_id  UUID NOT NULL,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_substations_geom ON raw_substations USING gist (geom);

-- Highways (motorway + trunk; proxy for fiber RoW)
CREATE TABLE IF NOT EXISTS raw_highways (
    id                BIGSERIAL PRIMARY KEY,
    osm_id            BIGINT,
    ref               TEXT,
    classification    TEXT,
    geom              geometry(LineString, 4326) NOT NULL,
    ingestion_run_id  UUID NOT NULL,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_highways_geom ON raw_highways USING gist (geom);

-- Railways
CREATE TABLE IF NOT EXISTS raw_railways (
    id                BIGSERIAL PRIMARY KEY,
    osm_id            BIGINT,
    geom              geometry(LineString, 4326) NOT NULL,
    ingestion_run_id  UUID NOT NULL,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_railways_geom ON raw_railways USING gist (geom);

-- Water bodies (rivers + named lakes/reservoirs from OSM)
CREATE TABLE IF NOT EXISTS raw_water_bodies (
    id                BIGSERIAL PRIMARY KEY,
    osm_id            BIGINT,
    name              TEXT,
    kind              TEXT,            -- 'river' | 'lake' | 'reservoir' | 'pond'
    geom              geometry(Geometry, 4326) NOT NULL,
    ingestion_run_id  UUID NOT NULL,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_water_geom ON raw_water_bodies USING gist (geom);

-- Protected areas (WDPA, India subset)
CREATE TABLE IF NOT EXISTS raw_protected_areas (
    id                BIGSERIAL PRIMARY KEY,
    wdpa_id           BIGINT,
    name              TEXT,
    designation       TEXT,
    iucn_cat          TEXT,
    geom              geometry(MultiPolygon, 4326) NOT NULL,
    ingestion_run_id  UUID NOT NULL,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_wdpa_geom ON raw_protected_areas USING gist (geom);

-- Submarine cable landing stations (curated static list)
CREATE TABLE IF NOT EXISTS raw_cable_landings (
    id                BIGSERIAL PRIMARY KEY,
    name              TEXT,
    city              TEXT,
    operators         TEXT[],
    cables            TEXT[],
    geom              geometry(Point, 4326) NOT NULL,
    ingestion_run_id  UUID NOT NULL,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_cable_landings_geom ON raw_cable_landings USING gist (geom);

-- Major metros (population anchor + latency demand center)
CREATE TABLE IF NOT EXISTS raw_metros (
    id                BIGSERIAL PRIMARY KEY,
    name              TEXT NOT NULL,
    population        BIGINT,
    state_code        TEXT,
    geom              geometry(Point, 4326) NOT NULL,
    ingestion_run_id  UUID NOT NULL,
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_metros_geom ON raw_metros USING gist (geom);

-- ============================================================================
-- Raster-derived zonal stats from GEE (one row per H3 cell per layer).
-- Generic shape: cell_id + numeric value(s) + lineage.
-- ============================================================================
CREATE TABLE IF NOT EXISTS raster_zonal_seismic (
    h3_id             h3index NOT NULL,
    resolution        SMALLINT NOT NULL,
    pga_g             REAL,                -- peak ground accel (g)
    in_zone_v         BOOLEAN,
    ingestion_run_id  UUID NOT NULL,
    PRIMARY KEY (h3_id, resolution)
);

CREATE TABLE IF NOT EXISTS raster_zonal_flood (
    h3_id             h3index NOT NULL,
    resolution        SMALLINT NOT NULL,
    occurrence_pct    REAL,                -- JRC GSW occurrence
    seasonality       REAL,
    ingestion_run_id  UUID NOT NULL,
    PRIMARY KEY (h3_id, resolution)
);

CREATE TABLE IF NOT EXISTS raster_zonal_slope (
    h3_id             h3index NOT NULL,
    resolution        SMALLINT NOT NULL,
    mean_slope_deg    REAL,
    max_slope_deg     REAL,
    p95_slope_deg     REAL,
    ingestion_run_id  UUID NOT NULL,
    PRIMARY KEY (h3_id, resolution)
);

CREATE TABLE IF NOT EXISTS raster_zonal_landcover (
    h3_id             h3index NOT NULL,
    resolution        SMALLINT NOT NULL,
    forest_pct        REAL,
    cropland_pct      REAL,
    urban_pct         REAL,
    water_pct         REAL,
    bareland_pct      REAL,
    wetland_pct       REAL,
    ingestion_run_id  UUID NOT NULL,
    PRIMARY KEY (h3_id, resolution)
);

CREATE TABLE IF NOT EXISTS raster_zonal_solar (
    h3_id             h3index NOT NULL,
    resolution        SMALLINT NOT NULL,
    pvout_kwh_per_kwp REAL,                -- annual yield
    ghi_kwh_per_m2    REAL,
    ingestion_run_id  UUID NOT NULL,
    PRIMARY KEY (h3_id, resolution)
);

CREATE TABLE IF NOT EXISTS raster_zonal_climate (
    h3_id             h3index NOT NULL,
    resolution        SMALLINT NOT NULL,
    mean_temp_c       REAL,
    mean_rh_pct       REAL,
    ingestion_run_id  UUID NOT NULL,
    PRIMARY KEY (h3_id, resolution)
);

CREATE TABLE IF NOT EXISTS raster_zonal_population (
    h3_id             h3index NOT NULL,
    resolution        SMALLINT NOT NULL,
    pop_total         REAL,
    pop_density_per_km2 REAL,
    ingestion_run_id  UUID NOT NULL,
    PRIMARY KEY (h3_id, resolution)
);

SET search_path TO dc_india, public;

-- ============================================================================
-- India administrative boundary (GADM L1 = states; merged for envelope use)
-- ============================================================================
CREATE TABLE IF NOT EXISTS india_states (
    state_code  TEXT PRIMARY KEY,
    state_name  TEXT NOT NULL,
    geom        geometry(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_india_states_geom ON india_states USING gist (geom);

-- ============================================================================
-- H3 cell tables. h3index is the native type from h3-pg (~8 bytes, btree).
-- Geometry materialized only for res 6 & 7 (rendering & spatial joins).
-- Res 8 stays geometry-free until drill-down explicitly requests it.
-- ============================================================================
CREATE TABLE IF NOT EXISTS h3_cells_res6 (
    h3_id          h3index PRIMARY KEY,
    state_code     TEXT NOT NULL,
    district_code  TEXT,
    area_km2       REAL,
    geom           geometry(Polygon, 4326) NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_h3_res6_state ON h3_cells_res6 (state_code);
CREATE INDEX IF NOT EXISTS ix_h3_res6_geom  ON h3_cells_res6 USING gist (geom);

CREATE TABLE IF NOT EXISTS h3_cells_res7 (
    h3_id          h3index PRIMARY KEY,
    parent_6       h3index NOT NULL,
    state_code     TEXT NOT NULL,
    district_code  TEXT,
    area_km2       REAL,
    geom           geometry(Polygon, 4326) NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_h3_res7_parent6 ON h3_cells_res7 (parent_6);
CREATE INDEX IF NOT EXISTS ix_h3_res7_state   ON h3_cells_res7 (state_code);
CREATE INDEX IF NOT EXISTS ix_h3_res7_geom    ON h3_cells_res7 USING gist (geom);

-- Res 8: geometry deferred. Only populated for drill-down hot cells.
CREATE TABLE IF NOT EXISTS h3_cells_res8 (
    h3_id          h3index PRIMARY KEY,
    parent_7       h3index NOT NULL,
    parent_6       h3index NOT NULL,
    state_code     TEXT NOT NULL,
    district_code  TEXT,
    area_km2       REAL,
    geom           geometry(Polygon, 4326),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_h3_res8_parent7 ON h3_cells_res8 (parent_7);
CREATE INDEX IF NOT EXISTS ix_h3_res8_parent6 ON h3_cells_res8 (parent_6);
CREATE INDEX IF NOT EXISTS ix_h3_res8_state   ON h3_cells_res8 (state_code);

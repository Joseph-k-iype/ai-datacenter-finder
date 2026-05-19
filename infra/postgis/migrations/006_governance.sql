SET search_path TO dc_india, public;

-- ============================================================================
-- Pipeline governance: lineage, schema contracts, dead-letter queue.
-- Every ingest/feature/score op opens a row in ingestion_runs and tags its
-- output rows with the same UUID, so any bad batch can be rolled back by
-- ingestion_run_id.
-- ============================================================================

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source            TEXT NOT NULL,         -- 'osm.power' / 'gee.flood' / etc.
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    status            TEXT NOT NULL DEFAULT 'running',  -- running|success|partial|failed
    row_count         BIGINT,
    rows_rejected     BIGINT DEFAULT 0,
    schema_hash       TEXT,                  -- hash of expected columns + types
    duration_seconds  REAL,
    notes             TEXT,
    upstream_source   TEXT                   -- URL / GEE asset / Overpass query hash
);
CREATE INDEX IF NOT EXISTS ix_runs_source ON ingestion_runs (source);
CREATE INDEX IF NOT EXISTS ix_runs_status ON ingestion_runs (status);
CREATE INDEX IF NOT EXISTS ix_runs_started ON ingestion_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS schema_contracts (
    source            TEXT PRIMARY KEY,
    expected_schema   JSONB NOT NULL,        -- Pandera schema serialized
    schema_hash       TEXT NOT NULL,
    version           INT NOT NULL DEFAULT 1,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    description       TEXT
);

CREATE TABLE IF NOT EXISTS dead_letter_queue (
    id                BIGSERIAL PRIMARY KEY,
    run_id            UUID REFERENCES ingestion_runs(run_id) ON DELETE CASCADE,
    source            TEXT NOT NULL,
    payload           JSONB NOT NULL,         -- the offending row
    error             TEXT NOT NULL,
    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_dlq_run    ON dead_letter_queue (run_id);
CREATE INDEX IF NOT EXISTS ix_dlq_source ON dead_letter_queue (source);

-- Data quality assertions (results of validators.py per run)
CREATE TABLE IF NOT EXISTS dq_check_results (
    id                BIGSERIAL PRIMARY KEY,
    run_id            UUID REFERENCES ingestion_runs(run_id) ON DELETE CASCADE,
    check_name        TEXT NOT NULL,
    passed            BOOLEAN NOT NULL,
    observed          JSONB,
    expected          JSONB,
    severity          TEXT NOT NULL DEFAULT 'warn',  -- info|warn|error
    checked_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_dq_run ON dq_check_results (run_id);

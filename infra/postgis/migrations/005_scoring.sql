SET search_path TO dc_india, public;

-- ============================================================================
-- Scoring outputs. One row per cell per weights-config per scoring run.
-- breakdown JSONB stores the per-criterion contributions so the UI can
-- explain WHY a given cell scored high.
-- ============================================================================
CREATE TABLE IF NOT EXISTS scoring_runs (
    score_run_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    weights_id        TEXT NOT NULL,         -- e.g. "default" / "tier4_focused"
    weights_payload   JSONB NOT NULL,        -- snapshot of the weights used
    resolution        SMALLINT NOT NULL,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    cells_scored      BIGINT
);

CREATE TABLE IF NOT EXISTS scores_res7 (
    h3_id             h3index NOT NULL,
    score_run_id      UUID NOT NULL REFERENCES scoring_runs(score_run_id) ON DELETE CASCADE,
    score             REAL NOT NULL,
    breakdown         JSONB NOT NULL,
    PRIMARY KEY (h3_id, score_run_id)
);
CREATE INDEX IF NOT EXISTS ix_scores7_run   ON scores_res7 (score_run_id);
CREATE INDEX IF NOT EXISTS ix_scores7_score ON scores_res7 (score DESC);

CREATE TABLE IF NOT EXISTS scores_res8 (
    h3_id             h3index NOT NULL,
    score_run_id      UUID NOT NULL REFERENCES scoring_runs(score_run_id) ON DELETE CASCADE,
    score             REAL NOT NULL,
    breakdown         JSONB NOT NULL,
    PRIMARY KEY (h3_id, score_run_id)
);
CREATE INDEX IF NOT EXISTS ix_scores8_run   ON scores_res8 (score_run_id);

-- Top-N rankings per state (post-diversity filter)
CREATE TABLE IF NOT EXISTS top_sites_res7 (
    score_run_id      UUID NOT NULL REFERENCES scoring_runs(score_run_id) ON DELETE CASCADE,
    state_code        TEXT NOT NULL,
    rank              SMALLINT NOT NULL,
    h3_id             h3index NOT NULL,
    score             REAL NOT NULL,
    centroid          geometry(Point, 4326) NOT NULL,
    PRIMARY KEY (score_run_id, state_code, rank)
);

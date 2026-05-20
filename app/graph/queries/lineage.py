"""Provenance / lineage queries.

These walk the graph from a Score back to every IngestionRun that
contributed inputs to it. The UI provenance page renders this as a tree.
"""
from __future__ import annotations


def score_provenance(h3_id: str, score_run_id: str):
    """One-step provenance: score → cell + scoring_run + weights."""
    cypher = """
    MATCH (s:Score {h3_id: $h3_id, score_run_id: $score_run_id})
    OPTIONAL MATCH (s)-[:USES_WEIGHTS]->(sr:ScoringRun)
    OPTIONAL MATCH (sr)-[:APPLIES]->(w:Weight)
    OPTIONAL MATCH (s)-[:DERIVED_FROM]->(c:Cell)
    RETURN s.score          AS score,
           s.breakdown_json  AS breakdown,
           sr.started_at     AS scoring_started,
           sr.weights_id     AS weights_id,
           collect({criterion: w.criterion, value: w.value}) AS weights,
           c.h3_id           AS h3_id,
           c.state_code      AS state_code
    """
    return cypher, {"h3_id": h3_id, "score_run_id": score_run_id}


def cell_to_ingestion_runs(h3_id: str):
    """Lineage walk: from a cell, find every IngestionRun referenced by
    its NEAREST_* / EXCLUDED_BY targets via 1–2 hops.

    Returns a per-source breakdown — the user can spot stale inputs at
    a glance ("seismic was ingested 90 days ago").
    """
    cypher = """
    MATCH (c:Cell {h3_id: $h3_id})
    OPTIONAL MATCH (c)-[:NEAREST_LINE|NEAREST_SUBSTATION|NEAREST_CABLE|NEAREST_METRO|NEAREST_WATER|NEAREST_HIGHWAY|NEAREST_RAILWAY|INSIDE|EXCLUDED_BY]->(target)
    OPTIONAL MATCH (target)-[:FROM]->(ir:IngestionRun)
    WITH DISTINCT ir
    WHERE ir IS NOT NULL
    OPTIONAL MATCH (ir)-[:VALIDATED_BY]->(sc:SchemaContract)
    RETURN ir.run_id         AS run_id,
           ir.source          AS source,
           ir.status          AS status,
           ir.started_at      AS started_at,
           ir.finished_at     AS finished_at,
           ir.row_count       AS row_count,
           ir.rows_rejected   AS rows_rejected,
           sc.schema_hash     AS schema_hash,
           sc.version         AS schema_version
    ORDER BY ir.finished_at DESC
    """
    return cypher, {"h3_id": h3_id}


def latest_ingestion_runs():
    """For dashboard: most recent IngestionRun per source."""
    cypher = """
    MATCH (ir:IngestionRun)
    WITH ir.source AS source, max(ir.finished_at) AS latest
    MATCH (ir:IngestionRun {source: source, finished_at: latest})
    RETURN ir.source AS source,
           ir.status AS status,
           ir.row_count AS row_count,
           ir.rows_rejected AS rows_rejected,
           ir.duration_seconds AS duration_seconds,
           ir.finished_at AS finished_at
    ORDER BY source
    """
    return cypher, {}


def dlq_summary():
    """DLQ counts by source — sanity check on data quality."""
    cypher = """
    MATCH (rr:RejectedRow)
    RETURN rr.source AS source, count(rr) AS n_rejected
    ORDER BY n_rejected DESC
    """
    return cypher, {}

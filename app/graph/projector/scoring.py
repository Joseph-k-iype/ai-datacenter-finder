"""Scoring projector (P4).

Reads from:
    dc_india.scoring_runs
    dc_india.scores_res7, scores_res8

Writes:
    (:ScoringRun {score_run_id})
    (:Weight {weight_key})            — one per (score_run, criterion)
    (:Score {score_key})              — one per (h3_id, score_run_id, resolution)
    (:ScoringRun)-[:APPLIES]->(:Weight)
    (:Score)-[:USES_WEIGHTS]->(:ScoringRun)
    (:Score)-[:DERIVED_FROM]->(:Cell)
    (:ScoringRun)-[:READS]->(:IngestionRun)   — links scoring to ingest lineage

This module also exposes ``hook_after_scoring_run()`` for incremental
projection from ``app/scoring/algorithm.py`` after a fresh score writes
its row to Postgres.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.core.db import session_scope
from app.core.logging import get_logger
from app.graph.client import batched_write, default_batch_size, query
from app.graph.schema import E, N

log = get_logger("graph.projector.scoring")

BATCH = default_batch_size()


def _project_scoring_runs() -> int:
    try:
        with session_scope() as session:
            rows = session.execute(
                text(
                    """
                    SELECT score_run_id, weights_id, weights_payload,
                           started_at, finished_at, cells_scored, resolution
                    FROM dc_india.scoring_runs
                    """
                )
            ).all()
    except ProgrammingError as exc:
        log.warning("graph.projector.scoring.runs.missing", error=str(exc))
        return 0

    run_rows = [
        {
            "score_run_id": str(r[0]),
            "weights_id": r[1],
            "started_at": r[3].isoformat() if r[3] is not None else None,
            "finished_at": r[4].isoformat() if r[4] is not None else None,
            "cells_scored": int(r[5]) if r[5] is not None else 0,
            "resolution": int(r[6]) if r[6] is not None else 7,
        }
        for r in rows
    ]
    if not run_rows:
        return 0

    query(
        f"UNWIND $rows AS r "
        f"MERGE (sr:{N.SCORING_RUN} {{score_run_id: r.score_run_id}}) "
        f"SET sr.weights_id = r.weights_id, sr.started_at = r.started_at, "
        f"    sr.finished_at = r.finished_at, sr.cells_scored = r.cells_scored, "
        f"    sr.resolution = r.resolution",
        {"rows": run_rows},
    )

    weight_rows: list[dict[str, Any]] = []
    for r in rows:
        run_id = str(r[0])
        payload = r[2]
        if not isinstance(payload, dict):
            try:
                payload = json.loads(payload) if payload else {}
            except (ValueError, TypeError):
                payload = {}
        for criterion, value in payload.items():
            if not isinstance(value, (int, float)):
                continue
            weight_rows.append(
                {
                    "weight_key": f"{run_id}::{criterion}",
                    "criterion": criterion,
                    "value": float(value),
                    "score_run_id": run_id,
                }
            )

    if weight_rows:
        query(
            f"UNWIND $rows AS r "
            f"MERGE (w:{N.WEIGHT} {{weight_key: r.weight_key}}) "
            f"SET w.criterion = r.criterion, w.value = r.value, "
            f"    w.score_run_id = r.score_run_id",
            {"rows": weight_rows},
        )
        query(
            f"UNWIND $rows AS r "
            f"MATCH (sr:{N.SCORING_RUN} {{score_run_id: r.score_run_id}}), "
            f"      (w:{N.WEIGHT} {{weight_key: r.weight_key}}) "
            f"MERGE (sr)-[:{E.APPLIES}]->(w)",
            {"rows": weight_rows},
        )

    return len(run_rows)


def _project_scores(resolution: int) -> int:
    """Stream score rows directly from Postgres into FalkorDB in batches.

    Avoids materialising the entire scores_res{R} table (~300k rows) into
    Python at once.
    """
    upsert = (
        f"UNWIND $rows AS r "
        f"MERGE (s:{N.SCORE} {{score_key: r.score_key}}) "
        f"SET s.h3_id = r.h3_id, s.score_run_id = r.score_run_id, "
        f"    s.score = r.score, s.breakdown_json = r.breakdown_json, "
        f"    s.resolution = r.resolution"
    )
    link_cell = (
        f"UNWIND $rows AS r "
        f"MATCH (s:{N.SCORE} {{score_key: r.score_key}}), "
        f"      (c:{N.CELL} {{h3_id: r.h3_id}}) "
        f"MERGE (s)-[:{E.DERIVED_FROM}]->(c) "
        f"MERGE (c)-[hs:{E.HAS_SCORE}]->(s) "
        f"SET hs.score_run_id = r.score_run_id"
    )
    link_run = (
        f"UNWIND $rows AS r "
        f"MATCH (s:{N.SCORE} {{score_key: r.score_key}}), "
        f"      (sr:{N.SCORING_RUN} {{score_run_id: r.score_run_id}}) "
        f"MERGE (s)-[:{E.USES_WEIGHTS}]->(sr)"
    )

    total = 0
    batch: list[dict[str, Any]] = []
    try:
        with session_scope() as session:
            result = session.execute(
                text(
                    f"""
                    SELECT h3_id::text, score_run_id, score, breakdown
                    FROM dc_india.scores_res{resolution}
                    """
                )
            ).yield_per(BATCH)
            for r in result:
                h3, score_run_id, score, breakdown = r
                if h3 is None or score_run_id is None or score is None:
                    continue
                if not isinstance(breakdown, dict):
                    try:
                        breakdown = json.loads(breakdown) if breakdown else {}
                    except (ValueError, TypeError):
                        breakdown = {}
                batch.append({
                    "score_key": f"{h3}::{score_run_id}::res{resolution}",
                    "h3_id": h3,
                    "score_run_id": str(score_run_id),
                    "score": float(score),
                    "breakdown_json": json.dumps(breakdown, default=str),
                    "resolution": resolution,
                })
                if len(batch) >= BATCH:
                    query(upsert, {"rows": batch})
                    query(link_cell, {"rows": batch})
                    query(link_run, {"rows": batch})
                    total += len(batch)
                    batch = []
    except ProgrammingError as exc:
        log.warning(
            "graph.projector.scoring.scores.missing",
            resolution=resolution,
            error=str(exc),
        )
        return 0

    if batch:
        query(upsert, {"rows": batch})
        query(link_cell, {"rows": batch})
        query(link_run, {"rows": batch})
        total += len(batch)
    return total


def project_scoring() -> dict[str, int]:
    counts: dict[str, int] = {}
    counts["scoring_runs"] = _project_scoring_runs()
    counts["scores_res7"] = _project_scores(7)
    counts["scores_res8"] = _project_scores(8)
    return counts


# ---------------------------------------------------------------------------
# Incremental hook — call from app/scoring/algorithm.py after writing a
# fresh ScoringRun + scores_res{R} batch.
# ---------------------------------------------------------------------------
def hook_after_scoring_run(score_run_id: str, resolution: int) -> dict[str, int]:
    """Project a single scoring run + its scores into FalkorDB.

    Cheaper than ``project_scoring()`` because it filters on score_run_id.
    """
    counts: dict[str, int] = {"scoring_runs": 0, "scores": 0}

    with session_scope() as session:
        run = session.execute(
            text(
                """
                SELECT score_run_id, weights_id, weights_payload,
                       started_at, finished_at, cells_scored, resolution
                FROM dc_india.scoring_runs
                WHERE score_run_id = :sid
                """
            ),
            {"sid": score_run_id},
        ).first()
    if run is None:
        log.warning("graph.projector.scoring.hook.missing_run", sid=score_run_id)
        return counts

    r = run
    query(
        f"MERGE (sr:{N.SCORING_RUN} {{score_run_id: $sid}}) "
        f"SET sr.weights_id = $wid, sr.started_at = $started, "
        f"    sr.finished_at = $finished, sr.cells_scored = $cnt, "
        f"    sr.resolution = $res",
        {
            "sid": str(r[0]),
            "wid": r[1],
            "started": r[3].isoformat() if r[3] is not None else None,
            "finished": r[4].isoformat() if r[4] is not None else None,
            "cnt": int(r[5]) if r[5] is not None else 0,
            "res": int(r[6]) if r[6] is not None else resolution,
        },
    )
    counts["scoring_runs"] = 1

    # Reuse the bulk projector but scoped — pull only this run's scores.
    with session_scope() as session:
        rows = session.execute(
            text(
                f"""
                SELECT h3_id::text, score_run_id, score, breakdown
                FROM dc_india.scores_res{resolution}
                WHERE score_run_id = :sid
                """
            ),
            {"sid": score_run_id},
        ).all()

    if not rows:
        return counts

    inserts = []
    for h3, srid, sc, bd in rows:
        if not isinstance(bd, dict):
            try:
                bd = json.loads(bd) if bd else {}
            except (ValueError, TypeError):
                bd = {}
        inserts.append(
            {
                "score_key": f"{h3}::{srid}::res{resolution}",
                "h3_id": h3,
                "score_run_id": str(srid),
                "score": float(sc),
                "breakdown_json": json.dumps(bd, default=str),
                "resolution": resolution,
            }
        )

    cypher = (
        f"UNWIND $rows AS r "
        f"MERGE (s:{N.SCORE} {{score_key: r.score_key}}) "
        f"SET s.h3_id = r.h3_id, s.score_run_id = r.score_run_id, "
        f"    s.score = r.score, s.breakdown_json = r.breakdown_json, "
        f"    s.resolution = r.resolution "
        f"WITH s, r "
        f"MATCH (c:{N.CELL} {{h3_id: r.h3_id}}), "
        f"      (sr:{N.SCORING_RUN} {{score_run_id: r.score_run_id}}) "
        f"MERGE (s)-[:{E.DERIVED_FROM}]->(c) "
        f"MERGE (s)-[:{E.USES_WEIGHTS}]->(sr) "
        f"MERGE (c)-[hs:{E.HAS_SCORE}]->(s) "
        f"SET hs.score_run_id = r.score_run_id"
    )
    with batched_write(N.SCORE, inserts, batch_size=BATCH) as chunks:
        for chunk in chunks:
            query(cypher, {"rows": chunk})
            counts["scores"] += len(chunk)

    return counts

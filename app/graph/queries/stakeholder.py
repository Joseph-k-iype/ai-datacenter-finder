"""Stakeholder-aware site queries.

Once Operator + SEZ + Hyperscaler nodes are populated, these queries
deliver the "AI data center site selection" insights that PostGIS alone
can't easily express.
"""
from __future__ import annotations


def top_sites_by_operator_type(
    score_run_id: str,
    operator_type: str,
    top_n: int = 50,
    min_score: float = 0.6,
):
    """Top-N cells whose nearest line is operated by an operator of the
    given type (psu/state/private).
    """
    cypher = """
    MATCH (c:Cell)-[:HAS_SCORE]->(s:Score {score_run_id: $score_run_id})
    WHERE s.score >= $min_score
    MATCH (c)-[:NEAREST_LINE]->(:Line)-[:OPERATED_BY]->(o:Operator {type: $type})
    RETURN c.h3_id AS h3_id, c.state_code AS state,
           s.score AS score, o.name AS operator
    ORDER BY s.score DESC
    LIMIT $top_n
    """
    return cypher, {
        "score_run_id": score_run_id,
        "type": operator_type,
        "top_n": int(top_n),
        "min_score": float(min_score),
    }


def cells_with_distinct_operator_redundancy(score_run_id: str, top_n: int = 20):
    """Cells where the dual-feed line is operated by a *different* operator
    than the nearest line — true operator-level redundancy.

    The dual-feed mechanism in cell_features only guarantees topological
    redundancy. This query layers operator distinctness on top — losing
    one operator's grid doesn't take both feeds.
    """
    cypher = """
    MATCH (c:Cell)-[:HAS_SCORE]->(s:Score {score_run_id: $score_run_id})
    MATCH (c)-[:NEAREST_LINE]->(l1:Line)-[:OPERATED_BY]->(o1:Operator)
    MATCH (c)-[:DUAL_FEED_LINE]->(l2:Line)-[:OPERATED_BY]->(o2:Operator)
    WHERE o1 <> o2
    RETURN c.h3_id      AS h3_id,
           c.state_code AS state,
           s.score      AS score,
           o1.name      AS primary_op,
           o2.name      AS backup_op
    ORDER BY s.score DESC
    LIMIT $top_n
    """
    return cypher, {"score_run_id": score_run_id, "top_n": int(top_n)}


def sites_near_sez(score_run_id: str, max_km: float = 25.0, min_score: float = 0.5):
    """Sites near an SEZ with data-center policy incentives.

    Filters to the same state first (cheap index hit on Cell.state_code)
    then approximates km distance from the cell centroid via Pythagorean
    degree distance — FalkorDB doesn't ship spatial functions yet. We
    convert degrees→km using the ~111 km/° equator approximation; for
    Indian latitudes the error is bounded under 1% which is fine for a
    "near" filter.

    For exact distance, the UI joins back to PostGIS using the returned
    h3_ids.
    """
    cypher = """
    MATCH (sez:SEZ {policy_tag: 'data_center_incentive'})
    WHERE sez.lat IS NOT NULL
    MATCH (c:Cell {state_code: sez.state_code})
    MATCH (c)-[:HAS_SCORE]->(s:Score {score_run_id: $score_run_id})
    WITH c, s, sez,
         sqrt((c.lat - sez.lat)^2 + (c.lon - sez.lon)^2) * 111.0 AS approx_km
    WHERE s.score >= $min_score AND approx_km <= $max_km
    RETURN c.h3_id AS h3_id, sez.name AS sez, s.score AS score, approx_km
    ORDER BY s.score DESC
    """
    return cypher, {
        "score_run_id": score_run_id,
        "max_km": float(max_km),
        "min_score": float(min_score),
    }


def hyperscaler_neighborhood(company: str, max_km: float = 100.0):
    """Cells within N km of any data center operated by the given company.

    ``company`` matches the canonicalized name produced by
    ``app/ingest/osm/data_centers.py`` (e.g. "Amazon Web Services").
    """
    cypher = """
    MATCH (h:Hyperscaler {company: $company})
    WHERE h.lat IS NOT NULL
    MATCH (c:Cell {state_code: h.state_code})
    WITH c, h, sqrt((c.lat - h.lat)^2 + (c.lon - h.lon)^2) * 111.0 AS approx_km
    WHERE approx_km <= $max_km
    OPTIONAL MATCH (c)-[:HAS_SCORE]->(s:Score)
    RETURN c.h3_id AS h3_id, c.state_code AS state, s.score AS score,
           h.display_name AS data_center, approx_km
    ORDER BY s.score DESC NULLS LAST
    """
    return cypher, {"company": company, "max_km": float(max_km)}

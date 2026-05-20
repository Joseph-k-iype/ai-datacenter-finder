"""Canonical resilience queries.

Each function returns ``(cypher, params)`` tuples that callers
(``app/ui/pages/6_Resilience.py``, the CLI, tests) can execute against
FalkorDB. Keeping the queries here ensures the UI and tests stay aligned.
"""
from __future__ import annotations


def cells_affected_by_substation_outage(osm_id: int, score_run_id: str | None = None):
    """Cells whose nearest line CONNECTS to the given substation.

    Optionally filter by a scoring run so we only return cells that
    currently make the top-N. If ``score_run_id`` is None, returns all
    cells regardless of score.
    """
    cypher = """
    MATCH (s:Substation {osm_id: $osm_id})<-[:CONNECTS]-(l:Line)<-[ne:NEAREST_LINE]-(c:Cell)
    OPTIONAL MATCH (c)-[:HAS_SCORE]->(score:Score)
    WHERE $score_run_id IS NULL OR score.score_run_id = $score_run_id
    RETURN c.h3_id      AS h3_id,
           c.state_code AS state_code,
           c.lat        AS lat,
           c.lon        AS lon,
           ne.km        AS line_km,
           score.score  AS score
    ORDER BY ne.km
    """
    return cypher, {"osm_id": int(osm_id), "score_run_id": score_run_id}


def cells_losing_dual_feed(subgrid_id: int):
    """Cells whose dual-feed line is in the given subgrid.

    If that subgrid goes down, these cells lose redundancy — they can
    still get power from their nearest line, but the topology-distinct
    second feed is gone.
    """
    cypher = """
    MATCH (g:SubGrid {subgrid_id: $subgrid_id})<-[:IN_SUBGRID]-(l:Line)<-[df:DUAL_FEED_LINE]-(c:Cell)
    RETURN c.h3_id      AS h3_id,
           c.state_code AS state_code,
           c.lat        AS lat,
           c.lon        AS lon,
           df.km        AS dual_km
    ORDER BY df.km
    """
    return cypher, {"subgrid_id": int(subgrid_id)}


def operator_concentration_per_top_score(score_run_id: str, top_n: int = 100):
    """Across the top-N scored cells, which operators dominate the supply?

    Surfaces concentration risk: if 80% of high-scoring cells depend on a
    single operator's line, that's a stakeholder risk worth flagging.
    """
    cypher = """
    MATCH (c:Cell)-[:HAS_SCORE]->(s:Score {score_run_id: $score_run_id})
    WITH c, s ORDER BY s.score DESC LIMIT $top_n
    MATCH (c)-[:NEAREST_LINE]->(l:Line)-[:OPERATED_BY]->(o:Operator)
    RETURN o.name AS operator, o.type AS type, count(c) AS n_cells
    ORDER BY n_cells DESC
    """
    return cypher, {"score_run_id": score_run_id, "top_n": int(top_n)}


def systemic_substations(min_affected: int = 25):
    """List substations that, if outaged, would affect more than N cells.

    Used to identify "hardening priorities" — substations that should
    receive redundant feeds or N+1 transformer designs.
    """
    cypher = """
    MATCH (s:Substation)<-[:CONNECTS]-(l:Line)<-[:NEAREST_LINE]-(c:Cell)
    WITH s, count(DISTINCT c) AS n_cells
    WHERE n_cells >= $min_affected
    RETURN s.osm_id AS osm_id, s.name AS name, s.voltage_kv AS voltage_kv,
           n_cells
    ORDER BY n_cells DESC
    """
    return cypher, {"min_affected": int(min_affected)}

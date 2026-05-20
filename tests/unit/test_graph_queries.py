"""Unit tests for canonical Cypher queries — string-only, no graph required.

We verify each query function returns a (cypher, params) tuple where:
  - cypher contains the expected MATCH/RETURN tokens
  - params keys line up with $placeholders in the cypher
This catches drift between callers (UI/CLI/tests) and the query library.
"""
from __future__ import annotations

import re

import pytest

from app.graph.queries import lineage, resilience, stakeholder


def _placeholders(cypher: str) -> set[str]:
    return set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)", cypher))


@pytest.mark.parametrize(
    "fn,kwargs,must_contain",
    [
        (
            resilience.cells_affected_by_substation_outage,
            {"osm_id": 12345, "score_run_id": "abc"},
            ["MATCH", "Substation", "CONNECTS", "NEAREST_LINE", "RETURN"],
        ),
        (
            resilience.cells_losing_dual_feed,
            {"subgrid_id": 7},
            ["SubGrid", "DUAL_FEED_LINE", "RETURN"],
        ),
        (
            resilience.systemic_substations,
            {"min_affected": 25},
            ["Substation", "CONNECTS", "count("],
        ),
        (
            resilience.operator_concentration_per_top_score,
            {"score_run_id": "abc", "top_n": 50},
            ["HAS_SCORE", "OPERATED_BY", "Operator"],
        ),
        (
            lineage.score_provenance,
            {"h3_id": "871234567ffffff", "score_run_id": "abc"},
            ["Score", "ScoringRun", "Cell"],
        ),
        (
            lineage.cell_to_ingestion_runs,
            {"h3_id": "871234567ffffff"},
            ["Cell", "IngestionRun", "SchemaContract"],
        ),
        (
            lineage.latest_ingestion_runs,
            {},
            ["IngestionRun", "max("],
        ),
        (
            lineage.dlq_summary,
            {},
            ["RejectedRow", "count("],
        ),
        (
            stakeholder.top_sites_by_operator_type,
            {"score_run_id": "abc", "operator_type": "private"},
            ["Operator", "OPERATED_BY", "HAS_SCORE"],
        ),
        (
            stakeholder.cells_with_distinct_operator_redundancy,
            {"score_run_id": "abc"},
            ["NEAREST_LINE", "DUAL_FEED_LINE", "OPERATED_BY"],
        ),
        (
            stakeholder.sites_near_sez,
            {"score_run_id": "abc"},
            ["SEZ", "Cell", "policy_tag"],
        ),
        (
            stakeholder.hyperscaler_neighborhood,
            {"company": "Amazon Web Services"},
            ["Hyperscaler", "Cell"],
        ),
    ],
)
def test_query_well_formed(fn, kwargs, must_contain):
    cypher, params = fn(**kwargs)
    assert isinstance(cypher, str)
    assert isinstance(params, dict)
    for tok in must_contain:
        assert tok in cypher, f"{fn.__name__} missing '{tok}' in cypher"
    # Every $placeholder must have a matching param.
    placeholders = _placeholders(cypher)
    missing = placeholders - set(params.keys())
    assert not missing, f"{fn.__name__}: $params not provided: {missing}"

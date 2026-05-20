"""Unit tests for the graph schema constants — pure-Python, no FalkorDB needed.

The risks we're guarding against:
  1. A typo in a node or edge label sneaks past code review (Cypher would
     happily MERGE a misspelled label into a brand-new node type).
  2. An IndexSpec drifts from its label.
  3. The exclusion-reason seed list is missing a name that the features
     layer emits, which would silently fail the MERGE-by-name edge build.
"""
from __future__ import annotations

import re

import pytest

from app.graph.schema import EXCLUSION_REASONS, INDEXES, E, N


def test_node_labels_are_uppercase_class_names():
    """Convention check: every Node label is PascalCase, since Cypher's
    convention is case-sensitive labels."""
    for attr, value in vars(N).items():
        if attr.startswith("_") or not isinstance(value, str):
            continue
        assert re.match(r"^[A-Z][A-Za-z]+$", value), (
            f"Node label {attr}={value!r} should be PascalCase"
        )


def test_edge_types_are_screaming_snake():
    """Cypher convention: edge types are UPPER_SNAKE_CASE."""
    for attr, value in vars(E).items():
        if attr.startswith("_") or not isinstance(value, str):
            continue
        assert re.match(r"^[A-Z_]+$", value), (
            f"Edge type {attr}={value!r} should be UPPER_SNAKE"
        )


def test_indexes_reference_known_labels():
    known = {v for v in vars(N).values() if isinstance(v, str) and not v.startswith("_")}
    for spec in INDEXES:
        assert spec.label in known, f"Index {spec} references unknown label"
        assert spec.property, f"Index {spec} missing property"


def test_index_create_cypher_is_well_formed():
    for spec in INDEXES:
        sql = spec.create_cypher()
        if spec.unique:
            assert sql.startswith("CREATE CONSTRAINT FOR")
            assert "IS UNIQUE" in sql
        else:
            assert sql.startswith("CREATE INDEX FOR")
        assert spec.label in sql
        assert spec.property in sql


def test_exclusion_reasons_complete():
    """Every reason name we MERGE on must be in the seed list."""
    required = {
        "seismic_zone_v",
        "flood_high",
        "slope_steep",
        "wdpa_intersect",
        "urban_dense",
        "forest_dense",
        "wetland",
        "waterbody",
    }
    seeded = {name for name, _ in EXCLUSION_REASONS}
    missing = required - seeded
    assert not missing, f"Exclusion reasons missing from seed: {missing}"


def test_no_duplicate_labels():
    seen = {}
    for attr, value in vars(N).items():
        if attr.startswith("_") or not isinstance(value, str):
            continue
        assert value not in seen, f"Duplicate node label {value}: {attr} & {seen[value]}"
        seen[value] = attr


@pytest.mark.parametrize(
    "edge",
    [E.OPERATED_BY, E.NEAREST_LINE, E.DUAL_FEED_LINE, E.CONNECTS, E.IN_SUBGRID],
)
def test_critical_edges_present(edge):
    """Spot-check that the edges queries depend on actually exist."""
    assert isinstance(edge, str) and edge

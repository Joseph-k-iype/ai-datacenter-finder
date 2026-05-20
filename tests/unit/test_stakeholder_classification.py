"""Operator + data-center classification heuristics.

The keyword maps in the stakeholder code are the user-visible behavior:
"is Adani classified as private?", "is PGCIL classified as a PSU?". If
someone removes a keyword by accident, the graph silently miscategorizes
thousands of edges — these tests pin the canonical mappings.
"""
from __future__ import annotations

import pytest

from app.graph.projector.stakeholder import _classify as classify_operator
from app.ingest.osm.data_centers import _classify_company, _classify_tier


@pytest.mark.parametrize(
    "raw,expected_type",
    [
        ("Power Grid Corporation of India", "psu"),
        ("POWERGRID", "psu"),
        ("PGCIL", "psu"),
        ("NTPC Ltd", "psu"),
        ("Maharashtra State Electricity Transmission Co Ltd", "state"),
        ("KPTCL", "state"),
        ("Tamil Nadu Transmission Corporation", "state"),
        ("Adani Transmission Ltd", "private"),
        ("Tata Power Company Ltd", "private"),
        ("Reliance Energy", "private"),
        ("Some Random Cooperative", "unknown"),
        ("", "unknown"),
    ],
)
def test_operator_classification(raw, expected_type):
    _, kind = classify_operator(raw)
    assert kind == expected_type, f"{raw!r} -> {kind} (expected {expected_type})"


def test_operator_canonical_name_preserved():
    name, _ = classify_operator("   Adani Transmission   ")
    assert name == "Adani Transmission"


@pytest.mark.parametrize(
    "name,operator,expected",
    [
        ("AWS Mumbai Region", None, "Amazon Web Services"),
        ("Amazon DC1", None, "Amazon Web Services"),
        ("Azure West India", None, "Microsoft Azure"),
        ("Yotta NM1", None, "Yotta Infrastructure"),
        ("Generic colo", None, None),
        (None, "Reliance Jio", "Reliance Jio"),
    ],
)
def test_data_center_company(name, operator, expected):
    assert _classify_company(name, operator) == expected


@pytest.mark.parametrize(
    "name,tags,expected",
    [
        ("Tier-III Data Center", {}, 3),
        ("Tier 4 facility", {}, 4),
        ("Some DC", {"tier": "Tier-II"}, 2),
        ("No tier here", {}, None),
    ],
)
def test_tier_classification(name, tags, expected):
    assert _classify_tier(name, tags) == expected

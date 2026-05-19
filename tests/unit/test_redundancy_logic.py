"""Dual-feed Tier-4 redundancy correctness — pure-Python algorithm test.

We can't easily test the PostGIS LATERAL DISTINCT ON query without a DB, but
we CAN test the upstream NetworkX sub-grid labeling on a synthetic graph.
"""
from __future__ import annotations

import networkx as nx


def _label_components(graph: nx.Graph) -> dict[str, int]:
    out: dict[str, int] = {}
    for idx, comp in enumerate(nx.connected_components(graph)):
        for node in comp:
            out[node] = idx
    return out


def test_two_lines_same_substation_same_component():
    """Two parallel lines, both connected to substation S1 → same component."""
    G = nx.Graph()
    G.add_node("sub_S1")
    G.add_node("cluster_A")
    G.add_node("cluster_B")
    G.add_edge("cluster_A", "sub_S1")
    G.add_edge("cluster_B", "sub_S1")
    comps = _label_components(G)
    assert comps["cluster_A"] == comps["cluster_B"]


def test_two_lines_distinct_substations_distinct_components():
    """Two lines to two unconnected substations → distinct components."""
    G = nx.Graph()
    G.add_node("sub_S1")
    G.add_node("sub_S2")
    G.add_node("cluster_A")
    G.add_node("cluster_B")
    G.add_edge("cluster_A", "sub_S1")
    G.add_edge("cluster_B", "sub_S2")
    comps = _label_components(G)
    assert comps["cluster_A"] != comps["cluster_B"]


def test_bridged_substations_collapse_components():
    """If a third line bridges S1↔S2, A and B end up in the same component."""
    G = nx.Graph()
    G.add_node("sub_S1")
    G.add_node("sub_S2")
    G.add_node("cluster_A")
    G.add_node("cluster_B")
    G.add_node("cluster_BRIDGE")
    G.add_edge("cluster_A", "sub_S1")
    G.add_edge("cluster_B", "sub_S2")
    G.add_edge("cluster_BRIDGE", "sub_S1")
    G.add_edge("cluster_BRIDGE", "sub_S2")
    comps = _label_components(G)
    assert comps["cluster_A"] == comps["cluster_B"]  # bridged

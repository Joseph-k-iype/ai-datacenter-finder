"""Knowledge-graph layer (FalkorDB).

Postgres is the source of truth; this package projects it into a property
graph that lets us ask questions PostGIS cannot answer cheaply:
cascade failure, multi-hop topology, lineage walk, stakeholder filters.

Entry points:
    from app.graph.client import get_graph
    from app.graph.projector.full_rebuild import rebuild_all

The graph is fully derivable from Postgres — `dc graph rebuild` regenerates
it from scratch. Incremental updates are written by hooks in
``app/governance/lineage.py`` and ``app/scoring/*`` when
``FALKORDB_AUTO_SYNC=true``.
"""
from __future__ import annotations

__all__ = ["client", "schema", "projector", "queries"]

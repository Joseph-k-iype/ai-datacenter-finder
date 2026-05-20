"""PostGIS → FalkorDB projectors.

Each module owns a slice of the graph (cells, power, lineage, scoring).
All projectors are MERGE-based and idempotent: running them twice yields
the same graph state.

Phasing matches the production rollout plan:
  - power: P1 — topology (Substations, Lines, SubGrids)
  - cells: P2 — H3 + state + protected areas + infrastructure distances
  - lineage: P3 — IngestionRun + SchemaContract + RejectedRow
  - scoring: P4 — ScoringRun + Score + Weight
  - stakeholder: P7 — Operator + SEZ + Hyperscaler
"""
from __future__ import annotations

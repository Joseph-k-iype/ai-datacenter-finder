# `app/graph/` — FalkorDB knowledge-graph layer

Project the PostGIS state into a queryable property graph. PostGIS stays
the source of truth; this package is a derived view rebuildable from
scratch via `dc graph rebuild`.

---

## Package layout

```
app/graph/
├── __init__.py
├── client.py                ← FalkorDB client singleton + batched_write helper
├── schema.py                ← N (node labels), E (edge types), INDEXES
├── parity.py                ← count diff vs Postgres; CLI `dc graph parity`
├── projector/               ← Postgres → FalkorDB writers (one per slice)
│   ├── full_rebuild.py      ← orchestrator; `dc graph rebuild`
│   ├── cells.py             ← Cell, State, ProtectedArea, Highway/...,
│   │                          NEAREST_*, EXCLUDED_BY, IN_STATE edges
│   ├── power.py             ← Substation, Line, SubGrid, CONNECTS,
│   │                          PARALLEL_TO, IN_SUBGRID
│   ├── lineage.py           ← IngestionRun, SchemaContract, RejectedRow
│   │                          + upsert_ingestion_run_node() hook
│   ├── scoring.py           ← ScoringRun, Score, Weight + hook_after_scoring_run
│   └── stakeholder.py       ← Operator (from OSM tags), SEZ, Hyperscaler
│
└── queries/                 ← Canonical Cypher library (UI + CLI + tests share)
    ├── resilience.py        ← outage simulation queries
    ├── lineage.py           ← provenance walks
    └── stakeholder.py       ← operator/SEZ/hyperscaler filters
```

---

## How to extend

Adding a new node type (worked example: adding `Airport`):

1. **`schema.py`** — add `AIRPORT = "Airport"` to `class N` and an
   `IndexSpec(N.AIRPORT, "iata", unique=True)` to `INDEXES`.
2. **`projector/cells.py`** (or a new module) — read from the relevant
   Postgres table, `UNWIND $rows AS r MERGE (a:Airport {iata: r.iata}) SET ...`.
3. **`projector/full_rebuild.py`** — register the new projector function
   alongside the others in the phase list.
4. **`queries/stakeholder.py`** (or a new module) — write canonical
   queries the UI consumes; never inline raw Cypher in the UI.
5. **`tests/unit/test_graph_schema.py`** — the existing parametrized
   tests already cover label conventions automatically; add cases for
   new edges in `test_critical_edges_present`.
6. **`tests/unit/test_graph_queries.py`** — add the new query to the
   parametrized `test_query_well_formed` list.

---

## Idempotency

Every projector uses MERGE (not CREATE), so re-runs converge:

```cypher
UNWIND $rows AS r
MERGE (c:Cell {h3_id: r.h3_id})
SET c.lat = r.lat, c.lon = r.lon, ...
```

The natural key (`h3_id` for cells, `osm_id` for OSM features,
`run_id` for ingestion runs) is enforced UNIQUE in `INDEXES`. Re-projecting
the entire graph from a fresh ingest produces an identical final state.

## Memory bounds

Projectors stream from Postgres to FalkorDB batch-by-batch rather than
loading the full table into Python. `projector/cells.py` and
`projector/scoring.py` use `session.execute(...).yield_per(BATCH)`
and flush each `BATCH` rows to FalkorDB before pulling the next, so
peak working set per projector is ~`batch_size` × ~16 properties
(typically <50 MB), not the full ~600 k res-7 cells × props at once.

When adding a new projector that reads a large table, mirror this
pattern — don't call `.all()` on the cursor and don't accumulate a
list across the entire scan.

---

## Two-mode sync

| Mode | Trigger | Coverage |
|---|---|---|
| **Bulk rebuild** | `dc graph rebuild` | All entities |
| **Incremental** | `ingestion_run` ctx exit, `score_cells` end | IngestionRun + ScoringRun + Score |

Incremental sync is best-effort (soft-fails on graph outage). Drift is
caught by `dc graph parity`, which counts both sides and exits non-zero
on > 0.5 % difference. Wire this into your monitoring.

---

## Why hybrid (graph + Postgres)

- PostGIS owns the raster zonal stats (`raster_zonal_*`) and the
  spatial-join workhorses. PostGIS is the right tool for that.
- FalkorDB owns the relationships — `(Cell)-[:NEAREST_LINE]->(Line)
  -[:CONNECTS]->(Substation)-[:IN_SUBGRID]->(SubGrid)`. The same
  question would need recursive CTEs in Postgres; in Cypher it's a
  one-liner.

This split means the UI mostly reads PostGIS (it knows the column layout)
but jumps to FalkorDB for the resilience / provenance / stakeholder
pages where graph traversal is dominant.

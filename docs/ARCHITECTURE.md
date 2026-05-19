# Architecture

This document explains the *why* behind every architectural decision. The
quick-reference layout is in the root [README](../README.md); start here
when you want to understand the reasoning.

---

## Table of contents

1. [Design tenets](#design-tenets)
2. [System diagram](#system-diagram)
3. [Spatial indexing: H3 + h3-pg](#spatial-indexing-h3--h3-pg)
4. [Storage: why PostGIS](#storage-why-postgis)
5. [Ingestion architecture](#ingestion-architecture)
6. [Governance (every ingest)](#governance-every-ingest)
7. [Feature computation](#feature-computation)
8. [Dual-feed redundancy: the topology trick](#dual-feed-redundancy-the-topology-trick)
9. [Scoring engine](#scoring-engine)
10. [Multi-resolution funnel](#multi-resolution-funnel)
11. [UI architecture](#ui-architecture)
12. [Scaling boundaries](#scaling-boundaries)
13. [Trade-offs explicitly rejected](#trade-offs-explicitly-rejected)

---

## Design tenets

1. **Authority over cleverness.** Every input is an authoritative source
   (GADM, JRC, ESA, NASA, OSM, WDPA, ECMWF). No predicted-from-imagery
   layers. State officials can audit every number back to a citable
   dataset.
2. **Governance from day 1.** Schema contracts, lineage tables, and a
   dead-letter queue exist before the first ingest. Adding governance
   later is a rewrite.
3. **Honest Tier-4 modeling.** "Two nearby HV lines" is a parallel
   right-of-way, not redundancy. Topology-aware sub-grid components are
   required.
4. **Pan-India from day 1.** No per-state special-casing. State-IT
   sessions pick a state in the UI; the data is already there.
5. **Config over code.** Every threshold, weight, asset ID lives in a
   YAML file. Code only changes when behaviour changes — not values.
6. **In-memory rescore.** Streamlit sliders must be sub-second. The
   feature table is read once at app start; the rescore is a pure
   function (`app/scoring/algorithm.py::score_dataframe`).

## System diagram

```
                  ┌──────────────────────────────────────────────────┐
                  │                EXTERNAL SOURCES                  │
                  ├──────────────────────────────────────────────────┤
                  │  Google Earth Engine (rasters, async export)     │
                  │  OpenStreetMap Overpass API (vectors)            │
                  │  GADM (boundary), curated lists (cables, metros) │
                  └────────────────────────┬─────────────────────────┘
                                           │
                  ┌────────────────────────▼─────────────────────────┐
                  │  INGEST ADAPTERS (app/ingest/)                   │
                  │                                                  │
                  │  GEE   : zonal_export.py → GCS CSV → DataFrame   │
                  │  OSM   : overpass.py     → parse → DataFrame     │
                  │  WDPA  : GEE export                              │
                  │  Static: YAML lists                              │
                  │                                                  │
                  │  Every adapter:                                  │
                  │   1. opens an ingestion_runs row (UUID)          │
                  │   2. validates via Pandera                       │
                  │   3. routes rejects to dead_letter_queue         │
                  │   4. closes the run with status + duration       │
                  └────────────────────────┬─────────────────────────┘
                                           │
                  ┌────────────────────────▼─────────────────────────┐
                  │  POSTGRES 16 + POSTGIS 3.4 + h3-pg 4.x           │
                  │                                                  │
                  │  schema dc_india:                                │
                  │   india_states     h3_cells_res{6,7,8}           │
                  │   raw_power_lines  raw_substations               │
                  │   raw_highways     raw_railways                  │
                  │   raw_water_bodies raw_protected_areas           │
                  │   raw_cable_landings  raw_metros                 │
                  │   raster_zonal_{seismic,flood,slope,landcover,   │
                  │     solar,climate,population}                    │
                  │   cell_features_res{7,8}                         │
                  │   scoring_runs  scores_res{7,8}  top_sites_res7  │
                  │   ingestion_runs schema_contracts dead_letter_q  │
                  │   dq_check_results                               │
                  └────────────────────────┬─────────────────────────┘
                                           │
                  ┌────────────────────────▼─────────────────────────┐
                  │  FEATURE / SCORING (app/features/, app/scoring/) │
                  │                                                  │
                  │  exclusion mask → distances → REDUNDANCY (the    │
                  │  LATERAL DISTINCT ON sub-grid query) → coverage  │
                  │  → climate/solar merge → weighted-sum score →    │
                  │  diversity-aware top-N                           │
                  └────────────────────────┬─────────────────────────┘
                                           │
                  ┌────────────────────────▼─────────────────────────┐
                  │  STREAMLIT UI (app/ui/)                          │
                  │                                                  │
                  │  Map / Tuner / Site Detail / Lineage / Compare   │
                  │  pydeck H3HexagonLayer + Plotly radar            │
                  │  in-memory Polars frame for live rescore         │
                  └──────────────────────────────────────────────────┘
```

## Spatial indexing: H3 + h3-pg

H3 (Uber's hexagonal hierarchical index) is preferred over rectangular
grids or administrative boundaries for three reasons:

- **Uniform area** — every res-7 cell is ~5.16 km², so scores compare
  apples to apples.
- **Equidistant neighbors** — every cell has exactly 6 neighbors at the
  same distance (the corner-case pentagons miss India entirely).
- **Hierarchical** — res-6 → res-7 → res-8 is a natural drill-down.

We use **the `h3-pg` PostgreSQL extension** (zachasme/h3-pg v4.2.x):

| Concern | Why h3-pg |
|---|---|
| Type safety | `h3index` is a binary 8-byte type. Mixing res-6 and res-7 IDs in a JOIN errors at the type system, not at runtime. |
| Indexing | btree on `h3index` is ~30 MB for 4.5 M cells. No GiST. |
| Hierarchy in SQL | `h3_cell_to_parent`, `h3_cell_to_children`, `h3_grid_disk` all run inside the query plan. |
| Polygon fill | `h3_polygon_to_cells(geom, res)` is set-returning and fast — we fill all of India one state at a time without touching Python. |
| Geometry on demand | `h3_cell_to_boundary_geometry` lets us materialize the GiST-indexed polygon only where rendering needs it; res-8 cells are geometry-free until drill-down. |

Why not just use Python `h3-py` + BIGINT columns? You lose every benefit
above — every hierarchy walk becomes a Python round-trip, and you can
accidentally JOIN cells across resolutions.

## Storage: why PostGIS

State IT organizations already run PostGIS for their land-records and
ULB GIS workflows. By matching their stack:

- **No new vendor dependency.** Handoff is `git clone` + `docker
  compose up`, not a SaaS subscription.
- **PostGIS KNN (`<->` on GiST).** Nearest-neighbor distance queries
  are constant-time-ish, which is essential when you do 644 k × millions.
- **Spatial joins** (ST_Intersects, ST_DWithin) — protected-area overlap
  testing is one CTE, not a Python loop.
- **LATERAL queries** — the redundancy `DISTINCT ON (subgrid_component)`
  pattern only works in a database with strong subquery support.
- **`ST_ClusterDBSCAN`** for parallel-circuit merging is built in.

We chose Postgres 16 (current LTS) + PostGIS 3.4 (current stable) +
`h3-pg` 4.2.x (current stable, h3 v4 API). The custom Docker image at
`infra/postgis/Dockerfile` builds `h3-pg` from source against the
official postgis image.

## Ingestion architecture

```
app/ingest/
  base.py              ← validate_and_split (Pandera → DLQ + clean DF)
  gee/
    client.py          ← ee.Initialize (SA preferred, user fallback)
    zonal_export.py    ← THE driver:
                          publishes once-uploaded H3 asset,
                          runs reduceRegions server-side,
                          Export.table.toCloudStorage (async),
                          polls task, reads CSV shards from GCS
    _common.py         ← upsert_zonal (bulk-insert into raster_zonal_*)
    seismic.py
    flood.py
    slope.py
    landcover.py
    solar.py
    climate.py
    population.py
  osm/
    overpass.py        ← rate-limited, retry, on-disk cache
    _writers.py        ← truncate + bulk insert helpers
    power.py
    power_topology.py  ← the Tier-4 keystone
    highways.py
    water.py
    railways.py
  wdpa/protected_areas.py
  static/
    cable_landings.py
    metros.py
```

**Pattern (every layer):**

1. Open an `ingestion_run` context manager — writes a UUID-tagged row to
   `ingestion_runs`, binds `pipeline_run_id` into the logger.
2. Load source data (GEE export poll, Overpass HTTP, or static YAML).
3. Build a Pandas DataFrame; pass through `validate_and_split` — clean
   rows go to the writer, rejects to `dead_letter_queue` with the
   Pandera failure trace.
4. Bulk-upsert via `app/core/db.py::bulk_execute` (parameterized SQL +
   executemany; psycopg3 sends it as one round-trip per chunk).
5. Context manager closes with status, row count, duration.

Why GEE export instead of inline `getInfo()`? Because at 644 k cells the
FeatureCollection geometry alone exceeds the 10 MB request-size cap, and
synchronous compute hits time limits. The async-export → GCS pattern is
what GEE itself documents for production zonal stats.

## Governance (every ingest)

Four tables form the audit substrate:

```sql
ingestion_runs       -- one row per ingest run, with status + duration
  (run_id PK, source, started_at, finished_at, status,
   row_count, rows_rejected, schema_hash, upstream_source, notes)

schema_contracts     -- the registered Pandera shape per source
  (source PK, expected_schema JSONB, schema_hash, version, updated_at)

dead_letter_queue    -- rejected rows, by run
  (id, run_id FK, source, payload JSONB, error, occurred_at)

dq_check_results     -- post-ingest DQ assertions
  (id, run_id FK, check_name, passed, observed, expected, severity)
```

The schema hash detects upstream drift. If OSM renames a tag or JRC ships
a new column, the hash changes and `dc validate` flags it before scores
can be computed against stale assumptions. Every ingested row carries the
`pipeline_run_id` so a bad batch is a one-line `DELETE WHERE
ingestion_run_id = …` rollback.

## Feature computation

```
app/features/
  build.py        ← orchestrator: per resolution × kind, dispatch in order
  exclusions.py   ← single big SQL UPSERT joining raster_zonal_* + WDPA
  distances.py    ← KNN <-> per target table → 7 columns
  redundancy.py   ← THE LATERAL DISTINCT ON sub-grid query
  coverage.py     ← merge landcover %s
  solar.py        ← merge solar / climate / population
```

All feature ops are *idempotent UPSERTs*. Re-running the pipeline never
loses data — it just refreshes.

## Dual-feed redundancy: the topology trick

This is the differentiator. Three files implement it together:

**Step 1 — DBSCAN parallel-circuit merge**
(`app/ingest/osm/power.py::parallel_circuit_cluster`):

```sql
ST_ClusterDBSCAN(geom, eps := 0.0045, minpoints := 1) OVER ()
```

ε = 0.0045° ≈ 500 m (configurable via
`configs/pipeline.yml::power_topology.parallel_cluster_eps_deg`). Parallel
HV circuits on the same tower set fall into one cluster_id.

**Step 2 — NetworkX component labeling**
(`app/ingest/osm/power_topology.py::label_subgrid_components`):

```
G: undirected graph
  nodes  = substations  (one per raw_substations row)
  edges  = clusters  (each cluster's line endpoints snap-to-nearest substation
                     within 1 km; that substation pair → an edge)

components = nx.connected_components(G)
  → each cluster_id receives a subgrid_component label
```

A subgrid_component is an **independent sub-grid** in the topology
sense: a fault in one cannot, by network reachability, propagate to
another in the same component.

The labels are bulk-applied to `raw_power_lines.subgrid_component`
via a temp staging table + JOIN UPDATE (one query, not 1 M).

**Step 3 — PostGIS query**
(`app/features/redundancy.py::compute_redundancy`):

```sql
WITH nearest_per_subgrid AS (
    SELECT g.h3_id, pl.subgrid_component,
           ST_DistanceSphere(ST_Centroid(g.geom), pl.geom)/1000.0 AS dist_km
    FROM dc_india.h3_cells_res7 g
    CROSS JOIN LATERAL (
        SELECT DISTINCT ON (subgrid_component)
               subgrid_component, geom
        FROM dc_india.raw_power_lines
        ORDER BY subgrid_component, ST_Centroid(g.geom) <-> geom
    ) pl
),
ranked AS (
    SELECT h3_id, dist_km,
           ROW_NUMBER() OVER (PARTITION BY h3_id ORDER BY dist_km) AS rk
    FROM nearest_per_subgrid
)
SELECT h3_id,
       MAX(CASE WHEN rk = 1 THEN dist_km END) AS nearest_hv_line_km,
       MAX(CASE WHEN rk = 2 THEN dist_km END) AS nearest_hv_line_distinct_subgrid_km
FROM ranked GROUP BY h3_id;
```

The `DISTINCT ON (subgrid_component)` ensures we keep one line per
distinct sub-grid; then `ROW_NUMBER()` gives 1st and 2nd nearest from
different sub-grids. This is Tier-4 honest.

## Scoring engine

```
app/scoring/
  transforms.py  ← pure-function normalizers → [0, 1]
                    exp_decay, sigmoid, linear_clamp,
                    plus composite builders for power & latency
  algorithm.py   ← score_dataframe (the pure function the UI rebinds to)
                    + score_cells (loads features, persists scores)
  ranking.py     ← diversity-aware top-N per state
  funnel.py      ← res-6 → res-7 → res-8 orchestrator
```

The **pure** `score_dataframe(df, weights_cfg) → DataFrame` is the
interface the Streamlit Tuner page rebinds to on every slider move. No
DB, no I/O. Sub-second rescore.

Composite score:

```
score = w_power * power_redundancy
      + w_water * water
      + w_conn  * connectivity
      + w_solar * solar
      + w_clim  * climate
      + w_lat   * latency
```

Each `sub_*` is in [0,1]; the JSONB `breakdown` column stores them per
cell so Site Detail can show the radar chart of *why* it scored that way.

Weights are normalized at score time (`_normalize_weights`) so users can
slide one without re-summing the rest.

## Multi-resolution funnel

The H3 hierarchy is not perfect spatial containment — children can spill
outside their parent. So features are **recomputed independently per
resolution from the raster**, never averaged up or down. The funnel uses
H3 hierarchy only as a *cell-ID filter*:

```
res-6 exclusion sweep
  ↓ (filters out ~80% of India: forests, urban, mountains, floodplains)
enumerate h3_cell_to_children(passing_res6, 7)
  ↓
res-7 features + redundancy + scoring
  ↓
take top-N res-7 cells (diversity-filtered)
  ↓
enumerate h3_cell_to_children(top_n_res7, 8)
  ↓
res-8 features + scoring on just those drill-down cells (~50k, not 4.5M)
```

This gives res-8 precision where it matters (top sites) without paying
res-8 cost everywhere (~10× more compute than res-7).

## UI architecture

```
app/ui/
  streamlit_app.py       ← landing page, theme, metrics
  _data.py               ← @st.cache_data loaders (5 min TTL)
  pages/
    1_Map.py             ← pydeck H3HexagonLayer + top-N markers
    2_Tuner.py           ← live weight sliders, in-memory rescore
    3_Site_Detail.py     ← per-cell radar + nearest infra
    4_Lineage.py         ← ingestion_runs, contracts, DLQ table
    5_Compare.py         ← side-by-side feature columns
  components/            ← (reserved for shared widgets)
  theme/                 ← (reserved for kepler config JSONs)
```

We use `pydeck` (the Python wrapper for deck.gl) rather than embedding
the Kepler.gl JS bundle — same H3HexagonLayer renderer, simpler bundle,
and tighter Streamlit data binding. The renderer choice is invisible to
users.

The **Tuner page** is the sales moment. It loads `cell_features_res7`
into a Pandas DataFrame at app start. Slider changes call
`score_dataframe()` (pure function), and the pydeck layer rebinds to the
new score column. No DB hit. Targeted latency: <500 ms per slider tick.

## Scaling boundaries

Tested mental-model numbers (validated by row-count bounds in
`configs/pipeline.yml::dq_row_count_bounds`):

| Step | Approximate scale | Compute notes |
|---|---|---|
| Grid build | 92 k res-6 + 644 k res-7 cells | One PG transaction per state, ~30 s total |
| GEE asset upload | ~5 min × number of chunks | Async; tasks visible in EE task manager |
| GEE zonal export (per layer) | 5–15 min wall, async | Server-side reduce; we poll every 30 s |
| OSM Overpass (per layer) | 30 s – 5 min, cached | On-disk cache by query hash |
| Feature compute (res 7) | ~5 min for all of India | All single-SQL updates with JOINs |
| Scoring (res 7) | ~1 min | Pure Pandas + one bulk insert chunked at 5k |
| Streamlit rescore | <500 ms | In-memory, no DB |

Concrete tuning hooks:
- `infra/tuning/postgresql.conf`: `shared_buffers`, `work_mem`,
  `max_parallel_workers_per_gather`.
- `configs/pipeline.yml::gee.export.tile_scale`: raise to 8 or 16 on
  "Computed value too large" GEE errors.
- `app/core/db.py::bulk_execute(chunk_size=...)`: default 5000;
  acceptable up to ~50k per chunk before psycopg buffer pressure.

## Trade-offs explicitly rejected

- **Vector tiles for the UI** — too heavy for a PoC; H3 hexagons are
  already the right primitive. We can layer in Mapbox vector tiles later
  if state offices need it.
- **A separate feature store (Feast / etc.)** — overkill at our cardinality.
  Postgres + JSONB breakdown is faster and more inspectable.
- **A custom orchestrator (Airflow / Prefect)** — the pipeline is linear and
  re-entrant; a CLI + Make is sufficient and runs in CI without a daemon.
- **Provider-specific cable maps (TeleGeography paid)** — the curated public
  list is enough for a PoC. Future: ingest from the paid API.
- **Live OSM updates via Overpass diffs** — the on-disk cache + monthly
  re-pull is fine for siting cadence. Live diffs aren't useful here.

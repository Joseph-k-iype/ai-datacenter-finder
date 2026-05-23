# 🛰  ai-data-center

**Pan-India Sovereign AI Infrastructure Siting** — a geospatial decision-support
system that scores every ~5 km² hexagon on the Indian subcontinent against
Uptime Institute Tier-4 AI data-center suitability criteria, with a real-time,
dark-mode, weight-tunable map for state-IT decision makers.

```
┌─────────────────────────────────────────────────────────────────────┐
│  India = ~3.28 M km²  ≈  92 k H3 res-6 cells                        │
│                          644 k H3 res-7 cells (main scoring grid)   │
│                        4.5 M H3 res-8 cells (drill-down per top-N)  │
│                                                                     │
│  Each cell is scored against 8 hard exclusions + 6 weighted         │
│  Tier-4 criteria including DUAL-FEED grid redundancy from           │
│  topologically-distinct sub-grids — not just "two nearby lines."    │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Table of contents

1. [Why this exists](#why-this-exists)
2. [What it does](#what-it-does)
3. [Quick start](#quick-start)
4. [Architecture overview](#architecture-overview)
5. [Repository layout](#repository-layout)
6. [The Tier-4 differentiator](#the-tier-4-differentiator-dual-feed-power-redundancy)
7. [Knowledge graph layer (FalkorDB)](#knowledge-graph-layer-falkordb)
8. [Data sources](#data-sources)
9. [Configuration](#configuration)
10. [Testing](#testing)
11. [Scaling considerations](#scaling-considerations)
12. [Roadmap](#roadmap--upgrade-paths)
13. [Documentation index](#documentation-index)

---

## Why this exists

India's AI compute commitments (10,000+ GPUs under IndiaAI, plus state-level
campuses) are outrunning land readiness. A 200 MW AI training facility requires:

- **Dual-redundant power feeds from topologically distinct sub-grids**
  (Tier-4 mandates 99.995 % uptime ≈ 26 min/year of permitted downtime).
- **Zero recurring flood exposure** (JRC GSW occurrence < 25 %).
- **Seismic isolation** (outside BIS Zone V / GSHAP PGA > 0.36 g).
- **Cooling water availability** (lake/river/treated effluent within ~10 km).
- **Dark-fiber adjacency** (Indian fiber backbones follow NHAI / rail RoW).
- **Latency to demand centers** (metros + submarine cable landings).
- **Optionally captive solar** for green PPA / carbon offset.

Identifying which ~3 % of pan-India hexagons satisfy these jointly is a six-week
GIS project per state. **This software collapses it to 30 seconds, country-wide.**

## What it does

- Builds a multi-resolution **H3 spatial index** (res 6 → 7 → 8 funnel) covering
  India from the GADM L1 boundary.
- Ingests **authoritative pan-India datasets** — Google Earth Engine rasters
  (JRC GSW, ESA WorldCover, SRTM, ERA5-Land, WorldPop, Global Solar Atlas),
  OpenStreetMap vectors (HV power, motorways, water, rail), WDPA protected
  areas, and curated submarine cable landings.
- Builds a NetworkX **sub-grid topology graph** from OSM power infrastructure
  so the dual-feed metric is topologically honest (parallel circuits on the
  same tower string do NOT count as redundant).
- Applies **hard-exclusion masks** (seismic / flood / slope / land-cover /
  protected-area / urban / wetland).
- Computes **smooth-transformed sub-scores** (exp-decay, sigmoid, linear
  clamps) and a **weighted composite** that the UI tunes live.
- Picks **diversity-aware top-N per state** (no two recommendations within
  50 km, so a single corridor doesn't dominate).
- Serves a **single-page kepler.gl + Streamlit** dark-mode UI: interactive
  H3 choropleth as the centerpiece, top-N callouts with per-site
  reasoning, drill-into-site breakdown, statistics + per-state aggregates
  — all on one screen with state filter, score threshold, and HV-grid
  overlay toggles in the sidebar.

## Quick start

```bash
# 1. One-time setup
cp .env.example .env                    # fill GEE SA JSON path, GCS bucket, PG password
make gee-auth                           # if using user OAuth instead of a service account
docker compose up -d                    # PostGIS + h3-pg + pgAdmin (3–5 min first build)
uv sync --extra dev                     # install Python deps

# 2. Build infrastructure
make init-db                            # apply migrations
make build-grid                         # ~92k res-6 + ~644k res-7 cells
make push-grid-to-gee                   # uploads cells as a GEE FeatureCollection asset

# 3. Ingest all data (~30–60 min wall, mostly GEE export polling)
make ingest-all                         # 7 GEE layers + 5 OSM layers + WDPA + curated

# 4. Validate & score
make validate                           # schema contracts + DQ checks
make compute-features                   # res-6 exclusion + res-7 full features
make score-default                      # res-7 scores + top_sites_res7;
                                        # auto-runs `make drilldown` (res-8 features
                                        # for children of top-N res-7 cells)

# 5. Knowledge graph (optional but recommended — powers Resilience /
#    Provenance / Stakeholder UI pages and the cascade-failure queries)
make graph-up                           # start FalkorDB container (Redis-protocol)
make graph-rebuild                      # project Postgres state into the graph (~5 min)
make graph-stats                        # confirm nodes + edges populated
make graph-parity                       # nonzero exit on drift > 0.5 %

# 6. Serve
make serve                              # streamlit on :8501 — single-page kepler.gl UI
# or, equivalently:
streamlit run app/ui/streamlit_app.py
```

Detailed walkthrough in [`docs/runbook.md`](docs/runbook.md).
Graph-layer specifics in [`docs/graph_runbook.md`](docs/graph_runbook.md).

## Architecture overview

```
                    ┌──────────────────────────────────────┐
                    │            DATA SOURCES              │
                    ├──────────────────────────────────────┤
   Google Earth     │  Rasters                             │
   Engine ─────────►│  - JRC Global Surface Water (flood)  │
                    │  - SRTM 30 m  (slope)                │
                    │  - ESA WorldCover 2021 (landcover)   │
                    │  - ERA5-Land  (temp / RH)            │
                    │  - WorldPop 100 m  (density)         │
                    │  - Global Solar Atlas (PVOUT)        │
                    │  - NASA SEDAC GSHAP PGA (seismic)    │
                    │  - WCMC WDPA  (protected areas)      │
                    ├──────────────────────────────────────┤
   OSM Overpass ───►│  Vectors                             │
                    │  - power=line voltage≥220 kV         │
                    │  - power=substation                  │
                    │  - highway=motorway|trunk            │
                    │  - waterway=river / natural=water    │
                    │  - railway=rail                      │
                    ├──────────────────────────────────────┤
   Curated  ───────►│  - Submarine cable landings          │
                    │  - Major metros + population         │
                    │  - GADM India L1 boundary            │
                    └────────────────┬─────────────────────┘
                                     │
                  ┌──────────────────▼────────────────────┐
                  │  GOVERNANCE (every ingest)            │
                  │                                       │
                  │  Pandera schemas → validate           │
                  │       ├─ pass → write w/ run_id       │
                  │       └─ fail → dead_letter_queue     │
                  │  schema_contracts (hash + version)    │
                  │  ingestion_runs (status, duration)    │
                  │  dq_check_results                     │
                  └──────────────────┬────────────────────┘
                                     │
                  ┌──────────────────▼────────────────────┐
                  │  PostGIS 16 + h3-pg                   │
                  │                                       │
                  │  raw_*          (vector layers)       │
                  │  raster_zonal_* (one row per H3 cell) │
                  │  h3_cells_res{6,7,8}                  │
                  └──────────────────┬────────────────────┘
                                     │
                  ┌──────────────────▼────────────────────┐
                  │  FEATURE BUILD                        │
                  │                                       │
                  │  • exclusions  (binary mask)          │
                  │  • distances   (PostGIS KNN <->)      │
                  │  • REDUNDANCY  (LATERAL DISTINCT ON   │
                  │                  subgrid_component)   │
                  │  • coverage / solar / climate merge   │
                  │                                       │
                  │  → cell_features_res{7,8}             │
                  └──────────────────┬────────────────────┘
                                     │
                  ┌──────────────────▼────────────────────┐
                  │  SCORING ALGORITHM                    │
                  │                                       │
                  │  smooth transforms → [0,1]            │
                  │   - exp_decay (power, water, conn)    │
                  │   - sigmoid   (solar PVOUT)           │
                  │   - clamp     (climate)               │
                  │   - composite (latency)               │
                  │                                       │
                  │  weighted sum + JSONB breakdown       │
                  │  diversity-aware top-N per state      │
                  │                                       │
                  │  → scores_res{7,8} + top_sites_res7   │
                  └──────────────────┬────────────────────┘
                                     │
                  ┌──────────────────▼────────────────────┐
                  │  STREAMLIT + kepler.gl  (dark theme)  │
                  │                                       │
                  │  Single-page UX:                      │
                  │   - interactive H3 choropleth         │
                  │   - top-N callouts w/ reasoning       │
                  │   - drill-into-site breakdown panel   │
                  │   - score histogram + per-state stats │
                  │   - HV-grid overlay toggle            │
                  └───────────────────────────────────────┘
```

Detailed architecture: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Repository layout

```
ai-data-center/
├── app/                     ← Python application (see app/README.md)
│   ├── cli.py               ← Typer entry: `dc <subcommand>`
│   ├── core/                ← config, db, h3 utils, gcs, logging
│   ├── grid/                ← H3 grid builder + GEE asset publisher
│   ├── ingest/              ← gee/, osm/, wdpa/, static/
│   ├── governance/          ← contracts, lineage, DLQ, validators
│   ├── features/            ← exclusions, distances, redundancy, …
│   ├── scoring/             ← transforms, algorithm, ranking, funnel
│   ├── graph/               ← FalkorDB projection (client, schema,
│   │                          projector/, queries/) — see app/graph/README.md
│   └── ui/                  ← Single-page Streamlit + kepler.gl
│                              (streamlit_app.py, _data.py)
│
├── infra/                   ← Infrastructure (see infra/README.md)
│   ├── postgis/             ← Dockerfile + SQL migrations 001-007
│   ├── falkordb/            ← Cypher index/constraint specs (informational)
│   └── tuning/              ← postgresql.conf for spatial workloads
│
├── configs/                 ← All tunables (see configs/README.md)
│   ├── pipeline.yml         ← bbox, resolutions, GEE assets, scale_m, etc.
│   ├── sources.yml          ← every URL / Overpass query / curated list
│   ├── exclusions.yml       ← hard-mask thresholds
│   └── weights/             ← default / tier4_focused / green_focused
│
├── tests/                   ← Tests (see tests/README.md)
│   ├── unit/                ← 94 tests; no docker required
│   ├── integration/         ← testcontainers; postgis + h3 needed
│   └── fixtures/
│
├── docs/                    ← Architecture, methodology, runbook, pitch
│   ├── ARCHITECTURE.md
│   ├── data_sources.md
│   ├── tier4_methodology.md
│   ├── runbook.md
│   └── pitch_notes.md
│
├── data/                    ← Gitignored except docs/
│   ├── raw/                 ← downloaded GeoTIFFs, OSM cache
│   ├── interim/             ← GEE-roundtrip Parquet/CSV
│   ├── processed/           ← feature snapshots
│   └── docs/DATA_DICTIONARY.md
│
├── notebooks/               ← Exploratory analysis (gitignored .ipynb_checkpoints/)
│
├── Makefile                 ← One-liner wrappers around `dc` CLI
├── docker-compose.yml       ← PostGIS + pgAdmin
├── pyproject.toml           ← uv-managed deps + Typer entry point
├── .env.example             ← All env vars documented
└── README.md                ← (this file)
```

## The Tier-4 differentiator: dual-feed power redundancy

Most siting tools compute "nearest HV transmission line" and stop. That's
**not** Tier-4 honest. Two parallel circuits on the same tower string share a
single right-of-way — a tornado, fire, or sabotage takes both. The Uptime
Institute audit fails.

This system computes `nearest_hv_line_distinct_subgrid_km` — the nearest HV
line whose **topological sub-grid component** differs from the nearest-overall
line's. The pipeline:

1. **Ingest** OSM `power=line` ways with voltage ≥ 220 kV (`raw_power_lines`).
2. **DBSCAN-cluster** parallel circuits within ~500 m so lines on a shared
   right-of-way collapse into a single `cluster_id`.
3. **Build a NetworkX graph** where substations are nodes and clusters are
   edges (a cluster's endpoints snap to substations within 1 km).
4. **Run `nx.connected_components`** — each component is an *independent
   sub-grid* (a topology-level outage in one does not propagate to another).
5. **Label** every `raw_power_lines` row with its `subgrid_component`.
6. **Query** in PostGIS with `LATERAL DISTINCT ON (subgrid_component)`,
   ranking distances and taking the 2nd smallest. This is mathematically
   "nearest line from a *different* sub-grid."

The Tier-4 power score:

```
power_redundancy =
    0.6 · exp(-nearest_hv_line_km / 15)
  + 0.4 · exp(-nearest_hv_line_distinct_subgrid_km / 30)
```

This is what makes the score defensible in an Uptime audit. Full methodology:
[`docs/tier4_methodology.md`](docs/tier4_methodology.md).

## Knowledge graph layer (FalkorDB)

The same Postgres state is also projected into a **FalkorDB knowledge graph**
so the system can answer questions PostGIS can't easily express:

- **Cascade failure simulation** — "If substation X fails, which top-N sites
  lose dual-feed status?" One-hop Cypher on
  `(:Substation)<-[:CONNECTS]-(:Line)<-[:NEAREST_LINE]-(:Cell)`.
- **Operator-aware redundancy** — Cells whose primary and dual-feed lines are
  operated by *different* operators (true grid-operator redundancy, not just
  topology).
- **Provenance walk** — From a `Score` node, walk back through `:DERIVED_FROM`,
  `:NEAREST_*`, `:FROM` edges to every `IngestionRun` that fed it. Surfaces
  stale-input warnings automatically.
- **Stakeholder filters** — Sites near a Karnataka SEZ flagged for data-center
  incentives, or sites within 50 km of an existing AWS / Azure footprint.

**Architecture is hybrid:** PostGIS remains the source of truth (raster zonal
stats stay there); the graph is a derived view, fully rebuildable via
`dc graph rebuild`. Lineage and scoring runs are also incrementally synced by
hooks in `app/governance/lineage.py` and `app/scoring/algorithm.py` —
soft-failing so a graph outage never breaks an ingest.

**No hardcoded data** — every entity (operators, SEZs, data centers) comes
from the same ingest layer as the rest of the system: OSM via Overpass for
SEZ + data-center polygons (`app/ingest/osm/sez.py`,
`app/ingest/osm/data_centers.py`); operator names are extracted from
existing `raw_power_lines.operator` and `raw_substations.operator`.

Bring it up:

```bash
make graph-up         # docker compose up -d falkordb
make graph-rebuild    # build the projection from Postgres
make graph-stats      # per-label node + per-type edge counts
make graph-parity     # drift check vs Postgres (nonzero exit on drift)
```

Streamlit pages added: **Resilience** (outage simulator), **Provenance**
(lineage walk + staleness dashboard), **Stakeholder** (operator / SEZ /
hyperscaler filters).

Details: [`app/graph/README.md`](app/graph/README.md) and
[`docs/graph_runbook.md`](docs/graph_runbook.md).

## Data sources

| Layer | Source | License |
|---|---|---|
| Boundary | GADM v4.1 L1 | Free for non-commercial |
| Seismic | NASA SEDAC GSHAP PGA | CC BY 4.0 |
| Flood | JRC Global Surface Water 1.4 | © EU 1995-2024 |
| Slope | SRTM 30 m | Public domain |
| Land cover | ESA WorldCover 2021 v200 | CC BY 4.0 |
| Protected | UNEP-WCMC WDPA | CC BY 4.0 |
| Power / Highway / Water / Rail | OpenStreetMap | ODbL |
| SEZs / Data centers | OpenStreetMap (`boundary=special_economic_zone`, `telecom|building=data_center`) | ODbL |
| Solar | Global Solar Atlas (fallback: ERA5-Land) | CC BY 4.0 (World Bank) |
| Climate | ECMWF ERA5-Land Monthly | © Copernicus |
| Population | WorldPop 100 m | CC BY 4.0 |
| Cable landings, Metros | Curated from public sources | (see configs/sources.yml) |

Full citations: [`docs/data_sources.md`](docs/data_sources.md).

## Configuration

**Nothing in this codebase is "magic-numbered."** Every threshold, weight,
distance, asset ID, and date range lives in one of:

| File | Controls |
|---|---|
| `.env` | secrets + endpoint URLs (PG creds, GEE SA JSON, GCS bucket) |
| `configs/pipeline.yml` | bbox, resolutions, GEE assets, scale_m, voltage thresholds, topology snap radii, DQ row-count bounds |
| `configs/exclusions.yml` | hard-mask thresholds (PGA, flood %, slope °, landcover %) |
| `configs/sources.yml` | URLs, Overpass queries, curated lists |
| `configs/weights/*.yml` | scoring weights + per-criterion transform params |

Three pre-built weight presets:
- `default.yml` — balanced
- `tier4_focused.yml` — heavy redundancy
- `green_focused.yml` — heavy solar / climate

Details: [`configs/README.md`](configs/README.md).

## Testing

```bash
make test                      # unit (20 tests, ~25 s, no docker)
make test-integration          # requires Docker (testcontainers)
make lint                      # ruff
make format                    # ruff format
```

Test layout: [`tests/README.md`](tests/README.md).

## Scaling considerations

Designed for pan-India scale on commodity hardware:

- **Bounded-memory ingestion.** Every GEE layer streams chunk-by-chunk via
  an `on_chunk` callback in `app/ingest/gee/zonal_export.py`: cells are
  pulled from Postgres lazily (`yield_per`), reduceRegions runs in a
  bounded-pipeline thread pool that caps in-flight chunks at
  `max_workers`, and each chunk is post-processed → validated →
  upserted → freed before the next one lands. Peak RAM ≈
  `max_workers × chunk_size` (~30 MB at defaults), not "all 600k rows ×
  all 6 layers in memory simultaneously."
- **Streaming graph projectors.** `app/graph/projector/cells.py` and
  `scoring.py` read Postgres via `yield_per(BATCH)` and flush each
  batch to FalkorDB before pulling the next — no full materialisation
  of the cell table in Python.
- **Bulk inserts everywhere.** No N+1 loops — `app/core/db.py::bulk_execute`
  wraps SQLAlchemy `text()` with `executemany` semantics; the topology
  UPDATE step uses a temp staging table + JOIN.
- **`h3-pg` native `h3index` type** with btree indexes — ~8 bytes/key,
  millions of cells in a few hundred MB.
- **Res-8 cells aren't materialized country-wide.** They're created on
  demand as children of top-N res-7 cells.
- **GEE server-side reduction** — the H3 grid is published once as a GEE
  asset (`grid push-to-gee`), then every `reduceRegions` references it by
  ID (avoids the 10 MB request-size cap).
- **PostGIS KNN (`<->`) on GiST indexes** keeps nearest-neighbor queries
  cheap even at 644 k × millions of features.
- **Cached UI loads.** Every Postgres read in the Streamlit app is
  wrapped with `st.cache_data(ttl=300)` so slider moves never re-hit
  the DB.

Concrete tuning hooks in `infra/tuning/postgresql.conf` (shared_buffers,
work_mem, parallel workers, JIT) and `configs/pipeline.yml::gee.export`
(chunk_size: 2500, max_workers: 4 — laptop-safe defaults; raise to
chunk_size 5000 + max_workers 16 on a workstation with 16+ GB free RAM).

## Roadmap / upgrade paths

- **State-DISCOM transmission ingests** (state-level GIS supersedes OSM)
- **CRZ-I coastline buffer** from MoEFCC shapefiles (current PoC leaves
  `coast_buffer_km` NULL; see `app/features/exclusions.py` docstring)
- **BIS IS-1893 zone-V raster** uploaded as GEE asset to replace GSHAP PGA
- **Submarine cable mapping** via paid TeleGeography data for richer
  latency modeling
- **DISCOM tariff layers** + state DC-policy incentive layers for
  cost-adjusted ranking
- **Lambert-conformal projection** for India to improve distance accuracy
- **Live weight tuning back in the UI** — the previous Tuner page was
  removed when consolidating to a single page; bring it back as a
  collapsible side panel that calls `score_dataframe()` on cached features
- **Selection-aware reasoning** — wire kepler.gl's `onHexagonClick` event
  into the right panel so clicking any hex (not just the top-N) populates
  the breakdown

## Documentation index

| Document | Purpose |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Deep architectural rationale + diagrams |
| [`docs/tier4_methodology.md`](docs/tier4_methodology.md) | Tier-4 scoring math + caveats |
| [`docs/data_sources.md`](docs/data_sources.md) | Source URIs, licenses, refresh cadence |
| [`docs/runbook.md`](docs/runbook.md) | Step-by-step setup & operations |
| [`docs/pitch_notes.md`](docs/pitch_notes.md) | Government-stakeholder framing |
| [`app/README.md`](app/README.md) | Python package map + conventions |
| [`infra/README.md`](infra/README.md) | Postgres image build + tuning |
| [`configs/README.md`](configs/README.md) | What each YAML key controls |
| [`tests/README.md`](tests/README.md) | How to run / what's mocked |
| [`data/docs/DATA_DICTIONARY.md`](data/docs/DATA_DICTIONARY.md) | Every column, units, source |

## License

PoC. External data sources retain their respective licenses (see
[`docs/data_sources.md`](docs/data_sources.md)). Code: choose one
appropriate to the recipient governmental body before redistribution.

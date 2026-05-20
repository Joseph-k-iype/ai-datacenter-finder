# `app/` — Python package map

This is the Python application. Everything below is in-process Python; the
only external dependencies are Postgres (running in Docker), GEE
(authenticated outside this codebase), and GCS (for GEE-export readback).

## Module map

```
app/
├── cli.py            ← Typer entry point. `dc <command>` (see pyproject `[project.scripts]`).
├── core/             ← cross-cutting concerns
│   ├── config.py     ← Pydantic Settings + YAML loaders. NO MODULE READS .env DIRECTLY.
│   ├── logging.py    ← structlog; every log line carries pipeline_run_id when bound.
│   ├── db.py         ← SQLAlchemy engine, session_scope(), bulk_execute(), apply_migrations(), check_health().
│   ├── gcs.py        ← GCS client for GEE-export readback (read_csv_glob).
│   └── h3_utils.py   ← Python-side H3 helpers (most H3 work happens in SQL via h3-pg).
│
├── grid/             ← H3 grid creation
│   ├── india_boundary.py ← GADM L1 download + cache; load into india_states table.
│   ├── builder.py        ← Populate h3_cells_res{6,7} via h3-pg's h3_polygon_to_cells.
│   │                       Res-8 cells materialize on-demand via populate_drilldown_cells().
│   └── gee_asset.py      ← Publish H3 cells as a GEE FeatureCollection (one-time per resolution).
│
├── ingest/           ← Data ingestion adapters
│   ├── base.py       ← validate_and_split (Pandera → DLQ + clean DF). Used by every adapter.
│   ├── gee/          ← Google Earth Engine zonal-stat adapters
│   │   ├── client.py        ← ee.Initialize wrapper (SA preferred, user fallback).
│   │   ├── zonal_export.py  ← THE driver. reduceRegions → Export.table.toCloudStorage → CSV → DF.
│   │   ├── _common.py       ← bulk-upsert into raster_zonal_* tables.
│   │   ├── seismic.py       ← NASA SEDAC GSHAP PGA → in_zone_v flag.
│   │   ├── flood.py         ← JRC GSW occurrence + seasonality.
│   │   ├── slope.py         ← SRTM 30m → mean/max/p95 slope.
│   │   ├── landcover.py     ← ESA WorldCover → forest/urban/water/wetland fractions.
│   │   ├── solar.py         ← Global Solar Atlas PVOUT (ERA5 fallback).
│   │   ├── climate.py       ← ERA5-Land mean temp + RH (Magnus formula).
│   │   └── population.py    ← WorldPop 100m → total + density per km².
│   ├── osm/          ← OpenStreetMap Overpass adapters
│   │   ├── overpass.py        ← rate-limited POST with retry + on-disk cache.
│   │   ├── _writers.py        ← shared insert/truncate helpers (bulk inserts).
│   │   ├── power.py           ← HV lines + substations + DBSCAN cluster_id.
│   │   ├── power_topology.py  ← THE Tier-4 keystone: NetworkX sub-grid components.
│   │   ├── highways.py        ← motorway + trunk.
│   │   ├── water.py           ← rivers + lakes/reservoirs.
│   │   ├── railways.py
│   │   ├── sez.py             ← Special Economic Zones (Overpass).
│   │   └── data_centers.py    ← Existing hyperscaler footprint (Overpass).
│   ├── wdpa/protected_areas.py ← WDPA via GEE; India subset.
│   └── static/
│       ├── cable_landings.py   ← curated submarine cable landings from sources.yml.
│       └── metros.py           ← curated metro list from sources.yml.
│
├── governance/       ← schema contracts, lineage, DLQ, DQ checks
│   ├── contracts.py  ← Pandera schemas; CONTRACTS dict registers them all.
│   ├── lineage.py    ← ingestion_run context manager (writes to ingestion_runs).
│   ├── dlq.py        ← dead_letter_queue push / push_many.
│   └── validators.py ← run_all_checks(); register_all_contracts().
│
├── features/         ← per-cell feature computation
│   ├── build.py      ← orchestrator. `compute_features(res, kind, top_n)`.
│   ├── exclusions.py ← single big SQL UPSERT (joins raster_zonal_* + WDPA).
│   ├── distances.py  ← PostGIS KNN `<->` per target table.
│   ├── redundancy.py ← LATERAL DISTINCT ON sub-grid for Tier-4 metric.
│   ├── coverage.py   ← merge landcover fractions.
│   └── solar.py      ← merge solar + climate + population.
│
├── scoring/          ← composite score + diversity-aware top-N
│   ├── transforms.py ← pure smooth normalizers (exp_decay / sigmoid / linear_clamp).
│   ├── algorithm.py  ← score_dataframe (PURE — the UI rebinds this); score_cells (persist).
│   ├── ranking.py    ← greedy diversity-aware top-N per state.
│   └── funnel.py     ← res-6 exclude → res-7 score → res-8 drill-down orchestrator.
│
├── graph/            ← FalkorDB knowledge-graph projection (see app/graph/README.md)
│   ├── client.py     ← client singleton + batched_write helper.
│   ├── schema.py     ← N (node labels), E (edge types), INDEXES.
│   ├── parity.py     ← count-diff vs Postgres (drift detector).
│   ├── projector/    ← Postgres → FalkorDB writers (full_rebuild orchestrator + per-slice modules).
│   └── queries/      ← canonical Cypher (UI + CLI + tests share).
│
└── ui/               ← Streamlit + pydeck
    ├── streamlit_app.py ← landing.
    ├── _data.py         ← @st.cache_data DB loaders.
    └── pages/           ← Streamlit auto-discovers numerically-prefixed files.
        ├── 1_Map.py
        ├── 2_Tuner.py
        ├── 3_Site_Detail.py
        ├── 4_Lineage.py
        ├── 5_Compare.py
        ├── 6_Resilience.py   ← outage simulator (FalkorDB-backed)
        ├── 7_Provenance.py   ← lineage walk + staleness dashboard
        └── 8_Stakeholder.py  ← operator / SEZ / hyperscaler filters
```

## Conventions

- **No SQL outside the data layer.** Modules that need DB access import
  `from app.core.db import session_scope, bulk_execute`. Don't construct
  engines elsewhere.
- **No env access outside `app/core/config.py`.** Everything routes through
  `get_settings()` so tests can override.
- **Every ingest runs inside an `ingestion_run(...)` context.** That's how
  the lineage table stays accurate. If you write an adapter that bypasses
  it, future audits will be wrong.
- **All bulk writes go through `bulk_execute`.** No `for row in rows:
  session.execute(...)` loops — they don't scale to 644 k cells.
- **Pandera contracts are mandatory.** Add a schema to
  `app/governance/contracts.py::CONTRACTS` before writing a new adapter.
- **Pure functions in `app/scoring/`.** `score_dataframe` and the
  `transforms` module must remain side-effect-free; the Streamlit tuner
  depends on it.

## Adding a new data layer (the recipe)

1. **Schema:** add a `DataFrameSchema` to `app/governance/contracts.py` and
   register it in `CONTRACTS`.
2. **Storage:** add the table to a new migration file under
   `infra/postgis/migrations/` (next available number).
3. **Adapter:** create `app/ingest/<source>/<layer>.py` with an `ingest()`
   function that:
   - Opens an `ingestion_run(source=..., upstream_source=..., schema_hash=...)`
   - Loads → DataFrame → `validate_and_split` → bulk insert.
4. **CLI wire-up:** add a case in `app/ingest/<source>/__init__.py::dispatch`,
   then a top-level Make target if it should run from `make ingest-all`.
5. **Feature use:** if the layer feeds scoring, add a column to
   `cell_features_res7` (next migration) and a writer in `app/features/`.
6. **Documentation:** update `docs/data_sources.md` and
   `data/docs/DATA_DICTIONARY.md`.

## CLI surface

Every command is in `cli.py`. Subcommand groups:

| Command | What it does |
|---|---|
| `dc init-db` | Apply migrations (idempotent). |
| `dc health` | Print DB connectivity + extension status as JSON. |
| `dc validate` | Run schema contracts + DQ checks. Exit 1 on errors. |
| `dc serve` | Launch Streamlit on :8501 (also `make serve`). |
| `dc score --weights F --res R --top-n N` | Score + pick top sites. |
| `dc grid build --res 6 --res 7 [--res 8]` | Populate H3 cells from GADM India. |
| `dc grid push-to-gee --res 7` | Upload H3 asset to GEE. |
| `dc ingest gee --layer {seismic\|flood\|slope\|landcover\|solar\|climate\|population}` | Single-layer GEE ingest. |
| `dc ingest osm --layer {power\|highways\|water\|railways} [--with-topology]` | Overpass ingest. |
| `dc ingest wdpa` | UNEP-WCMC protected areas via GEE. |
| `dc ingest static --layer {cable-landings\|metros}` | Curated lists. |
| `dc features compute --res R --kind {exclusion\|scoring\|all} [--top-n N]` | Build feature columns. |

Top-level Makefile targets bundle these — see `make help`.

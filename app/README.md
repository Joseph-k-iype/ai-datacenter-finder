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
│   │   ├── zonal_export.py  ← THE driver. Streams chunks via on_chunk callback —
│   │   │                       bounded-pipeline ThreadPoolExecutor caps in-flight
│   │   │                       requests at max_workers; cells pulled from PG via
│   │   │                       yield_per. Returns total row count (not a giant DF).
│   │   ├── _common.py       ← upsert_zonal: streams param rows via itertuples,
│   │   │                       no full to_dict copy. Safe to call per chunk.
│   │   ├── seismic.py       ← NASA SEDAC GSHAP PGA → in_zone_v flag.
│   │   ├── flood.py         ← JRC GSW occurrence + seasonality.
│   │   ├── slope.py         ← SRTM 30m → mean/max/p95 slope.
│   │   ├── landcover.py     ← ESA WorldCover → forest/urban/water/wetland fractions.
│   │   ├── solar.py         ← Global Solar Atlas PVOUT (ERA5 fallback).
│   │   ├── climate.py       ← ERA5-Land mean temp + RH (Magnus formula).
│   │   └── population.py    ← WorldPop 100m → total + density per km².
│   │   Every layer wires its post-processing → validate → upsert into an
│   │   on_chunk callback, so peak memory stays O(chunk_size).
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
└── ui/               ← Single-page Streamlit + kepler.gl
    ├── streamlit_app.py ← Whole app: kepler.gl H3 choropleth, top-N
    │                       callouts with per-site reasoning, drill-into-
    │                       site breakdown panel, score histogram + per-
    │                       state stats. Sidebar: state filter, min-score
    │                       slider, top-N count, HV-grid overlay toggle,
    │                       scoring-run picker.
    ├── _data.py          ← @st.cache_data Postgres loaders + the
    │                       reasoning_sentence()/summarise_breakdown()
    │                       helpers that explain why a site scored where
    │                       it did.
    └── _archive_pages/   ← Previous multi-page implementation preserved
                            here for reference; NOT loaded by Streamlit
                            (lives outside the conventional ./pages dir).
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
  `transforms` module must remain side-effect-free; an in-memory rescore
  panel depends on this purity.
- **Stream, don't materialize.** When pulling >50k rows from Postgres,
  use `session.execute(...).yield_per(BATCH)` and process batch-by-batch
  rather than `.all()`. Same applies to GEE chunks — go through
  `run_zonal_export(..., on_chunk=...)` so peak RAM stays bounded.

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
6. **Graph projection** (optional): if the layer represents an entity the
   resilience / lineage / stakeholder queries should see, add:
   - a node label to `app/graph/schema.py::N` + `IndexSpec` to `INDEXES`,
   - a projector function in `app/graph/projector/` reading from your
     new Postgres table,
   - the projector call in `app/graph/projector/full_rebuild.py`.
   The projector follows the standard `UNWIND $rows AS r MERGE ...` /
   `batched_write()` pattern — copy from `cells.py` or `power.py`.
7. **Documentation:** update `docs/data_sources.md` and
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
| `dc ingest osm --layer {power\|highways\|water\|railways\|sez\|data-centers} [--with-topology]` | Overpass ingest. |
| `dc ingest wdpa` | UNEP-WCMC protected areas via GEE. |
| `dc ingest static --layer {cable-landings\|metros}` | Curated lists. |
| `dc features compute --res R --kind {exclusion\|scoring\|all} [--top-n N]` | Build feature columns. |
| `dc graph health` | Ping FalkorDB; print node-count summary. |
| `dc graph rebuild [--reset] [--only PHASE ...]` | Rebuild FalkorDB projection from PostGIS. Idempotent. |
| `dc graph stats` | Per-label node + per-type edge counts (JSON). |
| `dc graph query CYPHER [--limit N]` | Run an ad-hoc Cypher; print rows as JSON. |
| `dc graph parity` | Compare counts vs Postgres; exit nonzero on drift > 0.5 %. |

Top-level Makefile targets bundle these — see `make help`. Graph
commands also have shortcuts: `make graph-up`, `make graph-rebuild`,
`make graph-stats`, `make graph-parity`, `make ingest-stakeholder`.

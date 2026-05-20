# `infra/` — Database Image + Migrations + Tuning

This is everything you'd hand to a state-IT DBA. The `docker-compose.yml`
at the repo root binds this in.

## Layout

```
infra/
├── postgis/
│   ├── Dockerfile             ← postgis:16-3.4 + h3-pg built from source
│   └── migrations/            ← applied automatically on container init
│       ├── 001_extensions.sql ← CREATE EXTENSION postgis, h3, h3_postgis
│       ├── 002_grid_tables.sql ← india_states + h3_cells_res{6,7,8}
│       ├── 003_raw_layers.sql ← all raw_* vector tables + raster_zonal_*
│       ├── 004_features.sql   ← cell_features_res{7,8} wide tables
│       ├── 005_scoring.sql    ← scoring_runs + scores_res{7,8} + top_sites_res7
│       ├── 006_governance.sql ← ingestion_runs + dlq + schema_contracts + dq_check_results
│       └── 007_stakeholder_layers.sql ← raw_sez + raw_data_centers (OSM-sourced)
├── falkordb/                  ← Knowledge-graph projection (Redis-protocol)
│   ├── README.md
│   └── migrations/001_indexes.cypher  ← informational; truth in app/graph/schema.py
└── tuning/
    └── postgresql.conf        ← shared_buffers, work_mem, parallel workers, JIT
```

## The custom image

The official `postgis/postgis:16-3.4` image doesn't ship `h3-pg`. Our
Dockerfile clones zachasme/h3-pg at a pinned tag (`H3_PG_VERSION` build
arg, default v4.2.2), builds it with CMake against the postgis image's
postgresql-server-dev headers, installs it, then drops the build deps.

The migrations directory is mounted at `/docker-entrypoint-initdb.d/` so
they auto-apply on **first** container init. To re-apply after a schema
change (assuming idempotent migrations — every one uses `IF NOT EXISTS`),
run `make init-db` which calls `dc init-db` → `apply_migrations()` in
`app/core/db.py`.

## Migration discipline

- **Number monotonically.** Next migration is `007_…sql`.
- **Use `IF NOT EXISTS` guards on every CREATE / ALTER.** Migrations must
  be safely re-runnable; we don't carry an applied-version table.
- **Never DROP in a migration.** If you need to change a column type,
  ALTER it; if you need to remove a table, do it in a one-off script and
  document why in `docs/runbook.md`.
- **`SET search_path TO dc_india, public;`** at the top of every file —
  the schema is set explicitly, not assumed from `current_database()`.

## Tuning

`infra/tuning/postgresql.conf` is mounted read-only into the container at
`/etc/postgresql/postgresql.conf`, and Postgres is started with
`-c config_file=/etc/postgresql/postgresql.conf`. Current values target a
laptop with ≥8 GB RAM:

| Setting | Value | Why |
|---|---|---|
| `shared_buffers` | 1 GB | Cache hot index blocks (h3_cells, GiST). |
| `work_mem` | 64 MB | Big-enough for the redundancy CTE per-cell hash. |
| `maintenance_work_mem` | 512 MB | CREATE INDEX, ANALYZE speed. |
| `effective_cache_size` | 3 GB | Hint for planner; assumes OS page cache. |
| `max_parallel_workers_per_gather` | 4 | KNN queries parallelize well. |
| `jit` | on | Big spatial joins benefit. |
| `log_min_duration_statement` | 1000 ms | Flag any unexpectedly slow query. |

For a 32 GB box, scale these up by 4×. For the eventual state-IT
production handoff, add `pg_stat_statements`, `pgaudit`, and connection
pooling (PgBouncer) — see `docs/ARCHITECTURE.md` for the upgrade path.

## Backups / DR

Not implemented in PoC. Production checklist for the handoff:

- `pg_dump --schema=dc_india` daily → object storage.
- Logical replica via `pglogical` or `wal2json` if there's a downstream
  consumer.
- Document RPO/RTO with state-IT before going live.

## pgAdmin

A `pgadmin` container runs alongside on :5050. Default credentials are
in `docker-compose.yml` (admin@example.com / admin). **Change these before
deploying anywhere reachable.**

## Smoke test

```bash
docker compose up -d postgis
docker compose logs -f postgis     # wait for "ready to accept connections"
dc health                          # should print {"postgres": "...", "postgis": true, "h3": true, "h3_postgis": true, "ok": true}
```

If any of `postgis | h3 | h3_postgis` is `false`, rebuild the image:

```bash
docker compose down -v             # ⚠ destroys data
docker compose build --no-cache postgis
docker compose up -d
make init-db
```

# FalkorDB graph — operator runbook

Quick reference for the knowledge-graph projection. PostGIS remains the
source of truth; this layer is a derived, queryable view.

---

## Bring-up (first time)

```bash
# 1. Add FalkorDB env vars to .env (already templated in .env.example).
# 2. Start the service.
make graph-up

# 3. Apply Postgres migrations (idempotent; includes new
#    stakeholder layer tables: raw_sez, raw_data_centers).
make init-db

# 4. (If not done already) ingest the full pipeline.
make ingest-all

# 5. Build the graph projection from the Postgres state.
make graph-rebuild
```

The first rebuild takes ~5–10 min on a ~600k-cell res-7 dataset.

---

## Health & drift monitoring

```bash
dc graph health      # ping FalkorDB; reports node count
dc graph stats       # per-label node + per-type edge counts
dc graph parity      # diff counts vs Postgres; exit nonzero on drift > 0.5%
```

Wire `dc graph parity` into a nightly cron. Drift indicates a missed
sync — re-run `make graph-rebuild` to repair.

---

## Incremental sync vs full rebuild

The graph is updated incrementally by hooks in:

- `app/governance/lineage.py::ingestion_run` — every ingest run upserts
  an `IngestionRun` node on start and updates it on finish.
- `app/scoring/algorithm.py::score_cells` — each scoring run upserts a
  `ScoringRun` + its `Score` nodes on completion.

Both hooks check `FALKORDB_AUTO_SYNC` (default `true`) and soft-fail —
a FalkorDB outage will never break ingest or scoring.

Other entity classes (Cell, Substation, Line, SubGrid, Operator, SEZ,
Hyperscaler) are populated only by `dc graph rebuild`. Run that after
any of:

- `make ingest-osm` (substations / lines / sez / data-centers changed)
- `make ingest-stakeholder` (SEZ / data-centers refreshed)
- A schema migration that touched `raw_*` columns

For cells specifically, `dc graph rebuild --only cells` is fast enough
to run on each features-compute completion.

---

## Common queries (Cypher)

### Substation outage impact

```cypher
MATCH (s:Substation {osm_id: $osm_id})<-[:CONNECTS]-(l:Line)<-[:NEAREST_LINE]-(c:Cell)
RETURN c.h3_id, c.state_code, c.lat, c.lon
ORDER BY c.h3_id
```

### Lineage walk for a single cell

```cypher
MATCH (c:Cell {h3_id: $h3_id})
OPTIONAL MATCH (c)-[:NEAREST_LINE|NEAREST_SUBSTATION|EXCLUDED_BY|INSIDE*1..2]->(t)
OPTIONAL MATCH (t)-[:FROM]->(ir:IngestionRun)-[:VALIDATED_BY]->(sc:SchemaContract)
RETURN DISTINCT ir.source, ir.finished_at, sc.schema_hash
ORDER BY ir.finished_at DESC
```

### Top operators by share of top-100 sites

```cypher
MATCH (c:Cell)-[:HAS_SCORE]->(s:Score {score_run_id: $sid})
WITH c, s ORDER BY s.score DESC LIMIT 100
MATCH (c)-[:NEAREST_LINE]->(:Line)-[:OPERATED_BY]->(o:Operator)
RETURN o.name, o.type, count(c) AS n
ORDER BY n DESC
```

Canonical versions of these are in `app/graph/queries/` — the UI and
CLI import from there. Don't inline raw Cypher in callers.

---

## Backup / restore

FalkorDB persists to `/data` in the container (volume
`falkordb-data`). The Redis snapshot mode is `--save 60 1000` (snapshot
if ≥1000 keys change in 60 s).

To take an explicit snapshot:

```bash
docker compose exec falkordb redis-cli BGSAVE
```

To restore on a new host: copy `dump.rdb` from the old volume into the
new container's `/data` before first start. Easier path: just run
`make graph-rebuild` — the graph is fully derivable from Postgres in
under 10 minutes, so backups are a convenience, not a requirement.

---

## Adding a new entity type

1. Add a constant to `app/graph/schema.py::N` (label) and / or `E` (edge type).
2. Add an `IndexSpec` to `INDEXES` in the same module.
3. Write a projector in `app/graph/projector/`. Follow the pattern in
   `cells.py`: `UNWIND $rows AS r MERGE ...` queries, `batched_write()`
   wrapper for throughput.
4. Register the projector in `app/graph/projector/full_rebuild.py`
   alongside the others.
5. Add a canonical query in `app/graph/queries/` if a UI page needs it.
6. Add unit tests under `tests/unit/test_graph_*` (schema + query
   well-formed assertions).
7. Update this runbook + the top-level README.

---

## Troubleshooting

**`dc graph rebuild` hangs or errors mid-phase**

The orchestrator soft-fails per phase — check the structlog output for
which phase failed and `dc graph rebuild --only <phase>` to retry just
that one. The parity command tells you exactly which entity types are
under-projected.

**Drift in parity check**

```bash
dc graph parity  # see which table is off
dc graph rebuild --only <phase>
```

A persistent drift means a write to Postgres bypassed the hook system.
Find the caller and route it through `ingestion_run` or `score_cells`.

**UI page shows "FalkorDB unreachable"**

```bash
docker compose ps falkordb       # is it up?
docker compose logs falkordb     # last error
dc graph health                  # python-side perspective
```

**Slow Cypher**

Check the index plan: `dc graph query "CALL db.indexes"`. Every MERGE
key must have a UNIQUE index. Missing index → full label scan → tens
of seconds per call.

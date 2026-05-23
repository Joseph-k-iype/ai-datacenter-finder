# `infra/falkordb/`

FalkorDB is a Redis-protocol property graph. We ship it as a single
Docker service (see `docker-compose.yml::falkordb`) with persistent
storage on a named volume.

## Files

```
infra/falkordb/
├── README.md                       ← this file
└── migrations/
    └── 001_indexes.cypher          ← authoritative index/constraint list
```

The Cypher in `migrations/001_indexes.cypher` is informational — the
executable source-of-truth is `app/graph/schema.py::INDEXES`, which
`app/graph/client.py::ensure_indexes()` applies on every rebuild. The
SQL-style migration file mirrors it so ops can read the schema without
opening Python.

## Operations

- Bring up: `make graph-up`
- Rebuild from Postgres: `make graph-rebuild`
- Health: `dc graph health`
- Drift check: `make graph-parity` (nonzero exit on drift > 0.5%)
- Ad-hoc query: `dc graph query "MATCH (n) RETURN labels(n), count(n)"`
- Workbench (browser UI): http://localhost:3000

Full runbook: [`docs/graph_runbook.md`](../../docs/graph_runbook.md).

## Capacity

For the pan-India dataset:

| Entity | Approx count |
|---|---|
| `Cell` (res 7) | ~600k |
| `Line` | ~25k |
| `Substation` | ~5k |
| `SubGrid` | ~500 |
| Edges (total) | ~3M |

In-memory footprint: well under 2 GB. Redis snapshot on disk: ~200 MB.
A laptop runs it fine; production is a single 4 GB container.

## Rebuild memory profile

`dc graph rebuild` reads Postgres in batches (`yield_per(BATCH)`) and
flushes each batch to FalkorDB before pulling the next, so the
projector's Python-side working set stays at ~`batch_size` × ~16
properties (<50 MB) regardless of how many cells the graph contains.
Tune `configs/pipeline.yml::graph.batch_size` if your FalkorDB build
benefits from a different chunk size.

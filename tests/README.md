# `tests/` — Testing strategy

```
tests/
├── conftest.py            ← shared fixtures (pg_container for integration)
├── unit/                  ← no docker; runs in ~25 s
│   ├── test_transforms.py     (scoring normalizers)
│   ├── test_h3_utils.py       (H3 helpers, no DB)
│   ├── test_contracts.py      (Pandera schemas)
│   └── test_redundancy_logic.py  (NetworkX sub-grid components)
├── integration/           ← requires docker (testcontainers spins postgis)
│   └── test_db_migrations.py
└── fixtures/              ← (reserved for small synthetic datasets)
```

## Running

```bash
make test                  # unit only (no docker)
make test-integration      # requires docker; spins a fresh postgis container
make lint                  # ruff
make format                # ruff format
```

Or directly:

```bash
uv run pytest tests/unit -v
uv run pytest tests/integration -v -m "not gee"
```

Markers (`pyproject.toml`):

| Marker | Meaning |
|---|---|
| `integration` | requires Postgres testcontainer |
| `gee` | requires real Google Earth Engine auth (skipped by default) |
| `slow` | takes >30 s |

## What's covered

### Unit (no docker)

- **`test_transforms.py`** — scoring normalizers. Validates:
  - `exp_decay(0) == 1`, `exp_decay(d) == 1/e`.
  - `sigmoid(center) == 0.5`.
  - `linear_clamp` clamps and inverts correctly.
  - **`power_redundancy_score` correctness**: a 2nd-feed from a different
    sub-grid pulls the score higher than the same-sub-grid case. This is
    the Tier-4 honesty check at the pure-function level.
  - `climate_score` is monotonic decreasing in temperature.
  - `latency_score` increases when both metro and cable are close.

- **`test_h3_utils.py`** — H3 helpers.
  - `cell_to_polygon` returns a 7-vertex (hexagon-closing) shape.
  - `parent`/`distance_km`/`cells_in_geometry` produce sane values.

- **`test_contracts.py`** — Pandera schemas.
  - Hash stability across two reads.
  - `osm.power_lines` rejects voltage < 220 kV.
  - `static.metros` rejects out-of-bbox coordinates.
  - All expected sources are in `CONTRACTS`.

- **`test_redundancy_logic.py`** — the sub-grid topology logic at the
  Python-graph level:
  - Two clusters connected to the same substation → same component.
  - Two clusters connected to different unconnected substations →
    different components.
  - A bridge cluster collapses two components into one.

### Integration (docker required)

- **`test_db_migrations.py`** — applies the spatial migrations against a
  bare `postgis/postgis:16-3.4` testcontainer. This is a thin smoke test
  for the SQL DDL; the full image (with `h3-pg`) is verified manually
  via `dc health` after `docker compose up`.

## What's NOT covered

- **End-to-end ingest with real GEE / Overpass** — requires creds; gate
  behind `@pytest.mark.gee`.
- **PostGIS KNN performance regression** — would need a representative
  dataset; track manually for now via `log_min_duration_statement` in
  `postgresql.conf`.
- **Streamlit UI tests** — out of scope for PoC; visual QA via runbook
  acceptance checks.

## Adding tests

Always start in `tests/unit/` if you can mock the DB. The
`validate_and_split` machinery is easily unit-tested by passing a
hand-crafted DataFrame; the `score_dataframe` function is pure.

For DB-touching logic, use the `pg_container` fixture in `conftest.py`.
Set `pytest.importorskip("testcontainers")` at the top to keep the
test runnable without docker.

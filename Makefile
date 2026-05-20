.PHONY: help install up down init-db build-grid gee-auth push-grid-to-gee \
        ingest-all ingest-fresh-all ingest-gee ingest-osm ingest-wdpa ingest-static \
        validate compute-features score-default serve \
        test test-unit test-integration lint format clean

PYTHON ?= python
DC ?= uv run python -m app.cli

# Set FRESH=1 on any `make ingest-*` invocation to bypass the skip-if-recent
# guard. Example:  make ingest-all FRESH=1
FRESH ?=
FRESH_FLAG := $(if $(FRESH),--fresh,)

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-22s %s\n", $$1, $$2}'

install: ## Install Python deps via uv
	uv sync --all-extras

up: ## Bring up PostGIS + pgAdmin
	docker compose up -d
	@echo "Waiting for postgres to be healthy..."
	@until docker compose ps postgis | grep -q "healthy"; do sleep 2; done
	@echo "postgres ready on :$${PG_PORT:-5432}"

down: ## Stop containers
	docker compose down

init-db: ## Apply migrations (idempotent; auto-runs on container first init)
	$(DC) init-db

build-grid: ## Populate h3_cells_res{6,7,8} from GADM India
	$(DC) grid build --res 6 --res 7 --res 8

gee-auth: ## Authenticate Google Earth Engine user credentials
	uv run earthengine authenticate --auth_mode=localhost:0 --force

push-grid-to-gee: ## OPTIONAL: upload h3 cells to GEE as an asset (not used by default ingest)
	$(DC) grid push-to-gee --res 7 --chunk-size 5000

ingest-gee: ## Ingest all GEE raster layers in one process (parallel chunks; FRESH=1 to force)
	$(DC) ingest gee-all $(FRESH_FLAG)

ingest-osm: ## Ingest all OSM vector layers
	$(DC) ingest osm --layer power --with-topology $(FRESH_FLAG)
	$(DC) ingest osm --layer highways $(FRESH_FLAG)
	$(DC) ingest osm --layer water $(FRESH_FLAG)
	$(DC) ingest osm --layer railways $(FRESH_FLAG)

ingest-wdpa: ## Ingest WDPA protected areas (India subset)
	$(DC) ingest wdpa $(FRESH_FLAG)

ingest-static: ## Load curated static lists (cable landings, metros)
	$(DC) ingest static --layer cable-landings $(FRESH_FLAG)
	$(DC) ingest static --layer metros $(FRESH_FLAG)

ingest-all: ingest-gee ingest-osm ingest-wdpa ingest-static ## All ingestion sources (idempotent)

ingest-fresh-all: ## Force re-ingest every source, bypassing the skip-if-recent guard
	$(MAKE) ingest-all FRESH=1

validate: ## Run schema contracts + DQ checks
	$(DC) validate

compute-features: ## res-6 exclusion → res-7 scoring features → res-8 drill-down
	$(DC) features compute --res 6 --kind exclusion
	$(DC) features compute --res 7 --kind all
	$(DC) features compute --res 8 --kind all --top-n 200

score-default: ## Score with default weights
	$(DC) score --weights configs/weights/default.yml --res 7

score-tier4: ## Score with Tier-4 heavy redundancy weights
	$(DC) score --weights configs/weights/tier4_focused.yml --res 7

serve: ## Launch Streamlit on :8501
	uv run streamlit run app/ui/streamlit_app.py

test: test-unit ## Run unit tests (no docker required)

test-unit:
	uv run pytest tests/unit -v

test-integration:
	uv run pytest tests/integration -v -m "not gee"

lint:
	uv run ruff check app tests

format:
	uv run ruff format app tests

clean:
	rm -rf data/raw/* data/interim/* data/processed/* .pytest_cache .ruff_cache .mypy_cache

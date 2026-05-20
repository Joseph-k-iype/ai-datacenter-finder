// Authoritative index/constraint list for the dc_india knowledge graph.
//
// This file is informational: `app/graph/client.py::ensure_indexes()` is
// the executable source-of-truth (Python keeps the labels in sync with
// app/graph/schema.py). Keep this file aligned for ops-readability.
//
// Conventions:
//   - Every label that participates in a MERGE has a UNIQUE constraint
//     on its natural key, backing idempotent re-projection.
//   - Foreign-key-like properties (state_code, subgrid_id) get a plain
//     index for fast traversal pre-filters.

// Cells (~600k @ res-7, larger @ res-8 drilldown subset).
CREATE CONSTRAINT FOR (n:Cell) REQUIRE n.h3_id IS UNIQUE;
CREATE INDEX FOR (n:Cell) ON (n.state_code);
CREATE INDEX FOR (n:Cell) ON (n.resolution);

// States + administrative roots.
CREATE CONSTRAINT FOR (n:State) REQUIRE n.state_code IS UNIQUE;

// Power infrastructure.
CREATE CONSTRAINT FOR (n:Substation) REQUIRE n.osm_id IS UNIQUE;
CREATE CONSTRAINT FOR (n:Line) REQUIRE n.osm_id IS UNIQUE;
CREATE INDEX FOR (n:Line) ON (n.subgrid_id);
CREATE CONSTRAINT FOR (n:SubGrid) REQUIRE n.subgrid_id IS UNIQUE;

// Supporting infrastructure.
CREATE CONSTRAINT FOR (n:ProtectedArea) REQUIRE n.wdpa_id IS UNIQUE;
CREATE CONSTRAINT FOR (n:Highway) REQUIRE n.osm_id IS UNIQUE;
CREATE CONSTRAINT FOR (n:Railway) REQUIRE n.osm_id IS UNIQUE;
CREATE CONSTRAINT FOR (n:WaterBody) REQUIRE n.osm_id IS UNIQUE;
CREATE CONSTRAINT FOR (n:CableLanding) REQUIRE n.landing_id IS UNIQUE;
CREATE CONSTRAINT FOR (n:Metro) REQUIRE n.metro_id IS UNIQUE;

// Stakeholder layer.
CREATE CONSTRAINT FOR (n:Operator) REQUIRE n.name IS UNIQUE;
CREATE CONSTRAINT FOR (n:SEZ) REQUIRE n.sez_id IS UNIQUE;
CREATE CONSTRAINT FOR (n:Hyperscaler) REQUIRE n.name IS UNIQUE;

// Lineage / governance.
CREATE CONSTRAINT FOR (n:IngestionRun) REQUIRE n.run_id IS UNIQUE;
CREATE CONSTRAINT FOR (n:SchemaContract) REQUIRE n.schema_hash IS UNIQUE;
CREATE CONSTRAINT FOR (n:RejectedRow) REQUIRE n.dlq_id IS UNIQUE;

// Scoring.
CREATE CONSTRAINT FOR (n:ScoringRun) REQUIRE n.score_run_id IS UNIQUE;
CREATE CONSTRAINT FOR (n:Score) REQUIRE n.score_key IS UNIQUE;
CREATE CONSTRAINT FOR (n:Weight) REQUIRE n.weight_key IS UNIQUE;

// Static reference vocab.
CREATE CONSTRAINT FOR (n:ExclusionReason) REQUIRE n.name IS UNIQUE;

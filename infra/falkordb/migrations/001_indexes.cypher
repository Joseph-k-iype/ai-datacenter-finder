// Authoritative index/constraint list for the dc_india knowledge graph.
//
// IMPORTANT: this file is informational only. FalkorDB does NOT accept
// the Neo4j-5 ``CREATE CONSTRAINT FOR ... REQUIRE ... IS UNIQUE`` Cypher
// syntax — its parser rejects it with
// ``Invalid input 'F': expected '=' or CREATE CONSTRAINT ON``.
//
// Constraints in FalkorDB are managed via the GRAPH.CONSTRAINT module
// commands (Redis-level, not Cypher). The Python wrapper used by
// ``app/graph/client.py::ensure_indexes()`` is
// ``graph.create_node_unique_constraint(label, prop)``.
//
// The executable source-of-truth is ``app/graph/schema.py::INDEXES``;
// this file mirrors it for ops-readability and to document the schema
// in a copy-paste-friendly form.
//
// Cypher snippets below ARE runnable as-is in FalkorDB (they create the
// underlying range indexes). The unique-constraint comments next to
// them describe the additional GRAPH.CONSTRAINT call we layer on top.

// ---------- Cells (~600k @ res-7, larger @ res-8 drilldown subset) ----------
CREATE INDEX FOR (n:Cell) ON (n.h3_id);           // + UNIQUE via GRAPH.CONSTRAINT
CREATE INDEX FOR (n:Cell) ON (n.state_code);
CREATE INDEX FOR (n:Cell) ON (n.resolution);

// ---------- States + administrative roots ----------
CREATE INDEX FOR (n:State) ON (n.state_code);     // + UNIQUE

// ---------- Power infrastructure ----------
CREATE INDEX FOR (n:Substation) ON (n.osm_id);    // + UNIQUE
CREATE INDEX FOR (n:Line) ON (n.osm_id);          // + UNIQUE
CREATE INDEX FOR (n:Line) ON (n.subgrid_id);
CREATE INDEX FOR (n:SubGrid) ON (n.subgrid_id);   // + UNIQUE

// ---------- Supporting infrastructure ----------
CREATE INDEX FOR (n:ProtectedArea) ON (n.wdpa_id);    // + UNIQUE
CREATE INDEX FOR (n:Highway) ON (n.osm_id);           // + UNIQUE
CREATE INDEX FOR (n:Railway) ON (n.osm_id);           // + UNIQUE
CREATE INDEX FOR (n:WaterBody) ON (n.osm_id);         // + UNIQUE
CREATE INDEX FOR (n:CableLanding) ON (n.landing_id);  // + UNIQUE
CREATE INDEX FOR (n:Metro) ON (n.metro_id);           // + UNIQUE

// ---------- Stakeholder layer ----------
CREATE INDEX FOR (n:Operator) ON (n.name);        // + UNIQUE
CREATE INDEX FOR (n:SEZ) ON (n.sez_id);           // + UNIQUE
CREATE INDEX FOR (n:Hyperscaler) ON (n.name);     // + UNIQUE

// ---------- Lineage / governance ----------
CREATE INDEX FOR (n:IngestionRun) ON (n.run_id);          // + UNIQUE
CREATE INDEX FOR (n:SchemaContract) ON (n.schema_hash);   // + UNIQUE
CREATE INDEX FOR (n:RejectedRow) ON (n.dlq_id);           // + UNIQUE

// ---------- Scoring ----------
CREATE INDEX FOR (n:ScoringRun) ON (n.score_run_id);   // + UNIQUE
CREATE INDEX FOR (n:Score) ON (n.score_key);           // + UNIQUE
CREATE INDEX FOR (n:Weight) ON (n.weight_key);         // + UNIQUE

// ---------- Static reference vocab ----------
CREATE INDEX FOR (n:ExclusionReason) ON (n.name);      // + UNIQUE

"""Node / edge label constants + index bootstrap.

Keeping every label as a Python constant makes typos a static-analysis
problem instead of a runtime one — Cypher will silently MERGE a typoed
label into a new (wrong) node type, so we never inline label strings.

Indexes are created once via ``ensure_indexes()`` (idempotent). FalkorDB
requires labelled-property indexes for fast lookup; without them every
MERGE is a full label scan.
"""
from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Node labels — every entity type that lives in the graph.
# ---------------------------------------------------------------------------
class N:
    CELL = "Cell"
    STATE = "State"
    SUBSTATION = "Substation"
    LINE = "Line"
    SUBGRID = "SubGrid"
    PROTECTED_AREA = "ProtectedArea"
    HIGHWAY = "Highway"
    RAILWAY = "Railway"
    WATER_BODY = "WaterBody"
    CABLE_LANDING = "CableLanding"
    METRO = "Metro"
    OPERATOR = "Operator"
    SEZ = "SEZ"
    HYPERSCALER = "Hyperscaler"
    INGESTION_RUN = "IngestionRun"
    SCHEMA_CONTRACT = "SchemaContract"
    REJECTED_ROW = "RejectedRow"
    SCORING_RUN = "ScoringRun"
    SCORE = "Score"
    WEIGHT = "Weight"
    EXCLUSION_REASON = "ExclusionReason"


# ---------------------------------------------------------------------------
# Edge types — verbs describing how entities relate.
# ---------------------------------------------------------------------------
class E:
    IN_STATE = "IN_STATE"
    HAS_SCORE = "HAS_SCORE"
    NEAREST_LINE = "NEAREST_LINE"
    DUAL_FEED_LINE = "DUAL_FEED_LINE"
    NEAREST_SUBSTATION = "NEAREST_SUBSTATION"
    NEAREST_CABLE = "NEAREST_CABLE"
    NEAREST_METRO = "NEAREST_METRO"
    NEAREST_WATER = "NEAREST_WATER"
    NEAREST_HIGHWAY = "NEAREST_HIGHWAY"
    NEAREST_RAILWAY = "NEAREST_RAILWAY"
    EXCLUDED_BY = "EXCLUDED_BY"
    INSIDE = "INSIDE"
    CHILD_OF = "CHILD_OF"
    CONNECTS = "CONNECTS"
    PARALLEL_TO = "PARALLEL_TO"
    IN_SUBGRID = "IN_SUBGRID"
    OPERATED_BY = "OPERATED_BY"
    FROM = "FROM"
    VALIDATED_BY = "VALIDATED_BY"
    REJECTED_BY = "REJECTED_BY"
    USES_WEIGHTS = "USES_WEIGHTS"
    APPLIES = "APPLIES"
    DERIVED_FROM = "DERIVED_FROM"
    READS = "READS"


@dataclass(frozen=True)
class IndexSpec:
    """Index / constraint declaration for a single (label, property) pair.

    FalkorDB does NOT accept the Neo4j-5 ``CREATE CONSTRAINT FOR ... REQUIRE
    ... IS UNIQUE`` Cypher syntax. Constraints there are a FalkorDB-native
    API (``graph.create_node_unique_constraint(label, *props)`` in the
    Python client). Plain indexes do work as Cypher.

    The ``apply()`` method below dispatches to the right API based on
    whether ``unique`` is set, so callers just call ``ensure_indexes()``
    without knowing the difference.
    """

    label: str
    property: str
    unique: bool = False

    def index_cypher(self) -> str:
        """Plain (non-unique) range index DDL — works in FalkorDB Cypher."""
        return f"CREATE INDEX FOR (n:{self.label}) ON (n.{self.property})"


# Index plan — every node label that participates in a lookup needs at
# least one index. Unique constraints back our MERGE-by-key semantics
# (so repeat projections stay idempotent).
INDEXES: list[IndexSpec] = [
    IndexSpec(N.CELL, "h3_id", unique=True),
    IndexSpec(N.CELL, "state_code"),
    IndexSpec(N.CELL, "resolution"),
    IndexSpec(N.STATE, "state_code", unique=True),
    IndexSpec(N.SUBSTATION, "osm_id", unique=True),
    IndexSpec(N.LINE, "osm_id", unique=True),
    IndexSpec(N.LINE, "subgrid_id"),
    IndexSpec(N.SUBGRID, "subgrid_id", unique=True),
    IndexSpec(N.PROTECTED_AREA, "wdpa_id", unique=True),
    IndexSpec(N.HIGHWAY, "osm_id", unique=True),
    IndexSpec(N.RAILWAY, "osm_id", unique=True),
    IndexSpec(N.WATER_BODY, "osm_id", unique=True),
    IndexSpec(N.CABLE_LANDING, "landing_id", unique=True),
    IndexSpec(N.METRO, "metro_id", unique=True),
    IndexSpec(N.OPERATOR, "name", unique=True),
    IndexSpec(N.SEZ, "sez_id", unique=True),
    IndexSpec(N.HYPERSCALER, "name", unique=True),
    IndexSpec(N.INGESTION_RUN, "run_id", unique=True),
    IndexSpec(N.SCHEMA_CONTRACT, "schema_hash", unique=True),
    IndexSpec(N.REJECTED_ROW, "dlq_id", unique=True),
    IndexSpec(N.SCORING_RUN, "score_run_id", unique=True),
    IndexSpec(N.SCORE, "score_key", unique=True),
    IndexSpec(N.WEIGHT, "weight_key", unique=True),
    IndexSpec(N.EXCLUSION_REASON, "name", unique=True),
]


# Canonical exclusion reasons. Pre-seeded so cells can reference them
# without each ingest needing to create-or-update them.
EXCLUSION_REASONS: list[tuple[str, str]] = [
    ("seismic_zone_v", "Seismic zone V (highest Indian hazard band)"),
    ("flood_high", "JRC GSW flood occurrence above threshold"),
    ("slope_steep", "Mean slope above threshold (cost & buildability)"),
    ("wdpa_intersect", "Overlaps a WDPA protected area"),
    ("urban_dense", "Urban landcover above threshold"),
    ("forest_dense", "Forest landcover above threshold"),
    ("wetland", "Wetland landcover above threshold"),
    ("waterbody", "Water-body landcover above threshold"),
]

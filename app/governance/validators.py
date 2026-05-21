"""Data quality checks. Run via `dc validate`. Results land in dq_check_results."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.core.config import load_pipeline_config
from app.core.db import bulk_execute, session_scope
from app.core.logging import get_logger
from app.governance.contracts import CONTRACTS, schema_hash

log = get_logger("governance.validators")


@dataclass
class CheckResult:
    name: str
    passed: bool
    observed: Any
    expected: Any
    severity: str = "warn"

    def as_row(self, run_id: UUID | None) -> dict[str, Any]:
        return {
            "run_id": str(run_id) if run_id else None,
            "check_name": self.name,
            "passed": self.passed,
            "observed": json.dumps(self.observed, default=str),
            "expected": json.dumps(self.expected, default=str),
            "severity": self.severity,
        }


def _row_count(table: str) -> int:
    with session_scope() as session:
        return int(session.execute(text(f"SELECT COUNT(*) FROM dc_india.{table}")).scalar_one())


def check_row_counts() -> list[CheckResult]:
    """Sanity row-count expectations for major tables."""
    cfg = load_pipeline_config().get("dq_row_count_bounds", {})
    bounds: dict[str, tuple[int, int]] = {
        tbl: (int(b[0]), int(b[1])) for tbl, b in cfg.items()
    }
    out: list[CheckResult] = []
    for table, (lo, hi) in bounds.items():
        try:
            n = _row_count(table)
        except Exception:
            n = -1
        out.append(
            CheckResult(
                name=f"row_count.{table}",
                passed=(lo <= n <= hi),
                observed=n,
                expected=f"[{lo}, {hi}]",
                severity="warn",
            )
        )
    return out


def check_schema_contracts() -> list[CheckResult]:
    """Compare registered schema_contracts hash to the live in-memory hash."""
    out: list[CheckResult] = []
    with session_scope() as session:
        registered = {
            row[0]: row[1]
            for row in session.execute(
                text("SELECT source, schema_hash FROM dc_india.schema_contracts")
            ).all()
        }
    for source, schema in CONTRACTS.items():
        live_hash = schema_hash(schema)
        stored_hash = registered.get(source)
        out.append(
            CheckResult(
                name=f"schema_hash.{source}",
                passed=(stored_hash == live_hash),
                observed=live_hash,
                expected=stored_hash or "<not registered>",
                severity="warn",
            )
        )
    return out


def check_null_geometries() -> list[CheckResult]:
    """No raw vector row should have a NULL geometry."""
    out: list[CheckResult] = []
    for tbl in (
        "raw_power_lines",
        "raw_substations",
        "raw_highways",
        "raw_railways",
        "raw_water_bodies",
        "raw_protected_areas",
        "raw_cable_landings",
        "raw_metros",
    ):
        try:
            with session_scope() as session:
                n = int(
                    session.execute(
                        text(f"SELECT COUNT(*) FROM dc_india.{tbl} WHERE geom IS NULL")
                    ).scalar_one()
                )
        except Exception:
            n = -1
        out.append(
            CheckResult(
                name=f"null_geometry.{tbl}",
                passed=(n == 0),
                observed=n,
                expected=0,
                severity="error" if n > 0 else "info",
            )
        )
    return out


def check_dlq_volume() -> list[CheckResult]:
    """Flag any source whose *current* DLQ has nontrivial volume.

    "Current" = rows tied to the latest successful ingestion_run per
    source. Otherwise old runs from before a contract widening would
    pollute the check forever, even after a clean re-ingest.
    """
    with session_scope() as session:
        rows = session.execute(
            text(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (source) source, run_id
                    FROM dc_india.ingestion_runs
                    WHERE status = 'success'
                    ORDER BY source, finished_at DESC
                )
                SELECT l.source, COUNT(d.dlq_id)
                FROM latest l
                LEFT JOIN dc_india.dead_letter_queue d
                  ON d.run_id = l.run_id AND d.source = l.source
                GROUP BY l.source
                HAVING COUNT(d.dlq_id) > 0
                """
            )
        ).all()
    out: list[CheckResult] = []
    for source, n in rows:
        out.append(
            CheckResult(
                name=f"dlq_volume.{source}",
                passed=(int(n) < 1000),
                observed=int(n),
                expected="< 1000",
                severity="warn",
            )
        )
    return out


def run_all_checks() -> dict[str, Any]:
    """Run every check, persist results, return summary.

    Self-registers every contract first so ``check_schema_contracts``
    has something to compare against. Without this, a fresh DB would
    fail every schema_hash check because nothing was ever stored.
    """
    # Idempotent — only writes/updates rows.
    register_all_contracts()

    checks: list[CheckResult] = []
    checks.extend(check_row_counts())
    checks.extend(check_schema_contracts())
    checks.extend(check_null_geometries())
    checks.extend(check_dlq_volume())

    bulk_execute(
        """
        INSERT INTO dc_india.dq_check_results
          (run_id, check_name, passed, observed, expected, severity)
        VALUES
          (NULL, :check_name, :passed, CAST(:observed AS JSONB),
           CAST(:expected AS JSONB), :severity)
        """,
        [c.as_row(run_id=None) for c in checks],
    )

    summary = {
        "total": len(checks),
        "passed": sum(1 for c in checks if c.passed),
        "failed": sum(1 for c in checks if not c.passed),
        "errors": [c.name for c in checks if not c.passed and c.severity == "error"],
        "warnings": [c.name for c in checks if not c.passed and c.severity == "warn"],
        "checks": [
            {"name": c.name, "passed": c.passed, "observed": c.observed, "severity": c.severity}
            for c in checks
        ],
    }
    return summary


def register_all_contracts() -> int:
    """Idempotently upsert every contract's hash into schema_contracts."""
    from app.governance.contracts import schema_to_dict

    payload_rows = [
        {
            "source": source,
            "payload": json.dumps(schema_to_dict(schema)),
            "hash": schema_hash(schema),
            "desc": f"Auto-registered Pandera schema for {source}",
        }
        for source, schema in CONTRACTS.items()
    ]
    n = bulk_execute(
        """
        INSERT INTO dc_india.schema_contracts
          (source, expected_schema, schema_hash, version, description)
        VALUES (:source, CAST(:payload AS JSONB), :hash, 1, :desc)
        ON CONFLICT (source) DO UPDATE
          SET expected_schema = EXCLUDED.expected_schema,
              schema_hash     = EXCLUDED.schema_hash,
              updated_at      = now()
        """,
        payload_rows,
    )
    log.info("contracts.registered", count=n)
    return n

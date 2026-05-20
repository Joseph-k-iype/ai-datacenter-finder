"""Score provenance — walk the graph from a Score back to every
IngestionRun that contributed inputs. Surfaces staleness and DLQ counts.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import streamlit as st

from app.graph.client import health as graph_health
from app.graph.client import query_rows
from app.graph.queries import lineage as Q
from app.ui._data import get_latest_run_id

st.set_page_config(
    page_title="Provenance • DC Hotspots",
    layout="wide",
    page_icon="🧬",
)
st.title("🧬 Score provenance walk")
st.caption(
    "Every score is derived from a chain of IngestionRuns. This page "
    "walks the graph — pick a cell, see the upstream sources, the schema "
    "contracts they validated against, and any stale inputs."
)

h = graph_health()
if not h.get("ok"):
    st.error("FalkorDB unreachable. `make graph-up && make graph-rebuild`.")
    st.json(h)
    st.stop()

st.success(f"FalkorDB connected — {h['node_count']:,} nodes")

score_run_id = get_latest_run_id()

# ---------------------------------------------------------------------------
# Pipeline-wide staleness dashboard.
# ---------------------------------------------------------------------------
st.subheader("Pipeline health — latest IngestionRun per source")
cypher, params = Q.latest_ingestion_runs()
try:
    rows = query_rows(cypher, params)
except Exception as exc:  # noqa: BLE001
    st.error(f"Graph query failed: {exc}")
    rows = []
runs = pd.DataFrame(
    rows,
    columns=[
        "source",
        "status",
        "row_count",
        "rows_rejected",
        "duration_seconds",
        "finished_at",
    ],
)
if not runs.empty:
    runs["finished_at"] = pd.to_datetime(runs["finished_at"], errors="coerce", utc=True)
    now_utc = datetime.now(UTC)
    runs["age_days"] = (now_utc - runs["finished_at"]).dt.total_seconds() / 86400.0
    runs["stale"] = runs["age_days"] > 14
    st.dataframe(
        runs,
        use_container_width=True,
        hide_index=True,
        column_config={
            "age_days": st.column_config.NumberColumn("Age (days)", format="%.1f"),
            "stale": st.column_config.CheckboxColumn("Stale (>14d)"),
        },
    )

# ---------------------------------------------------------------------------
# Per-cell provenance walk.
# ---------------------------------------------------------------------------
st.subheader("Walk a single cell's provenance")
h3_id = st.text_input("H3 cell id (res 7)", value="")
if not h3_id:
    st.info("Enter a cell id to walk its lineage. Use the Site Detail page to find one.")
    st.stop()

if score_run_id:
    sc_cypher, sc_params = Q.score_provenance(h3_id, score_run_id)
    try:
        sc_rows = query_rows(sc_cypher, sc_params)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Graph query failed: {exc}")
        sc_rows = []
    if sc_rows:
        score, breakdown_json, scoring_started, weights_id, weights, _h3, state = sc_rows[0]
        c1, c2, c3 = st.columns(3)
        c1.metric("Score", f"{score:.3f}" if score is not None else "—")
        c2.metric("Weights profile", weights_id or "—")
        c3.metric("State", state or "—")

        if weights:
            wdf = pd.DataFrame(weights)
            wdf = wdf[wdf["criterion"].notna()]
            if not wdf.empty:
                st.markdown("**Weights applied to this score**")
                st.dataframe(wdf, use_container_width=True, hide_index=True)

ir_cypher, ir_params = Q.cell_to_ingestion_runs(h3_id)
try:
    ir_rows = query_rows(ir_cypher, ir_params)
except Exception as exc:  # noqa: BLE001
    st.error(f"Graph query failed: {exc}")
    ir_rows = []
ingest_df = pd.DataFrame(
    ir_rows,
    columns=[
        "run_id",
        "source",
        "status",
        "started_at",
        "finished_at",
        "row_count",
        "rows_rejected",
        "schema_hash",
        "schema_version",
    ],
)
st.markdown("**Upstream ingestion runs feeding this cell**")
if ingest_df.empty:
    st.info("No ingestion runs found in the graph for this cell's upstream sources.")
else:
    ingest_df["finished_at"] = pd.to_datetime(
        ingest_df["finished_at"], errors="coerce", utc=True
    )
    cutoff = datetime.now(UTC) - timedelta(days=14)
    ingest_df["stale"] = ingest_df["finished_at"].fillna(cutoff - timedelta(days=1)) < cutoff
    st.dataframe(ingest_df, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# DLQ summary.
# ---------------------------------------------------------------------------
st.subheader("Dead-letter queue — rejected rows per source")
dlq_cypher, dlq_params = Q.dlq_summary()
try:
    dlq_rows = query_rows(dlq_cypher, dlq_params)
except Exception as exc:  # noqa: BLE001
    st.error(f"Graph query failed: {exc}")
    dlq_rows = []
dlq_df = pd.DataFrame(dlq_rows, columns=["source", "n_rejected"])
if dlq_df.empty:
    st.success("No DLQ rows — every input validated cleanly.")
else:
    st.dataframe(dlq_df, use_container_width=True, hide_index=True)

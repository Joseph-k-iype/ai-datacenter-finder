"""Stakeholder-aware site queries.

Filter top sites by operator type, dual-operator redundancy, SEZ
proximity, or hyperscaler adjacency. All queries run against FalkorDB.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from app.graph.client import health as graph_health
from app.graph.client import query_rows
from app.graph.queries import stakeholder as Q
from app.ui._data import get_latest_run_id

st.set_page_config(
    page_title="Stakeholder filters • DC Hotspots",
    layout="wide",
    page_icon="🤝",
)
st.title("🤝 Stakeholder-aware site queries")
st.caption(
    "Layer operator type, SEZ policy, and existing data-center proximity "
    "onto the score. Returns from the FalkorDB projection."
)

h = graph_health()
if not h.get("ok"):
    st.error("FalkorDB unreachable. `make graph-up && make graph-rebuild`.")
    st.json(h)
    st.stop()

score_run_id = get_latest_run_id()
if not score_run_id:
    st.warning("No scoring runs yet. Run `dc score` first.")
    st.stop()

st.caption(f"Active scoring run: `{score_run_id}`")

tabs = st.tabs(
    ["By operator type", "Distinct-operator redundancy", "Near SEZ", "Near hyperscaler"]
)

# ---------------------------------------------------------------------------
# By operator type
# ---------------------------------------------------------------------------
with tabs[0]:
    op_type = st.selectbox(
        "Operator type",
        options=["private", "psu", "state", "unknown"],
        index=0,
    )
    c1, c2 = st.columns(2)
    with c1:
        min_score = st.slider("Min score", 0.0, 1.0, 0.6, 0.05, key="op_score")
    with c2:
        top_n = st.slider("Top N", 5, 200, 50, 5, key="op_top")

    cypher, params = Q.top_sites_by_operator_type(
        score_run_id, op_type, top_n=top_n, min_score=min_score
    )
    try:
        rows = query_rows(cypher, params)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Graph query failed: {exc}")
        rows = []
    df = pd.DataFrame(rows, columns=["h3_id", "state", "score", "operator"])
    st.dataframe(df, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# Distinct-operator redundancy
# ---------------------------------------------------------------------------
with tabs[1]:
    st.markdown(
        "Cells whose primary and dual-feed lines are operated by **different** "
        "operators — losing one operator's grid doesn't kill both feeds."
    )
    top_n = st.slider("Top N", 5, 100, 20, 5, key="dist_top")
    cypher, params = Q.cells_with_distinct_operator_redundancy(score_run_id, top_n=top_n)
    try:
        rows = query_rows(cypher, params)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Graph query failed: {exc}")
        rows = []
    df = pd.DataFrame(rows, columns=["h3_id", "state", "score", "primary_op", "backup_op"])
    st.dataframe(df, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# Near SEZ
# ---------------------------------------------------------------------------
with tabs[2]:
    c1, c2 = st.columns(2)
    with c1:
        max_km = st.slider("Max km from SEZ", 5.0, 100.0, 25.0, 5.0)
    with c2:
        min_score = st.slider("Min score", 0.0, 1.0, 0.5, 0.05, key="sez_score")
    cypher, params = Q.sites_near_sez(score_run_id, max_km=max_km, min_score=min_score)
    try:
        rows = query_rows(cypher, params)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Graph query failed: {exc}")
        rows = []
    df = pd.DataFrame(rows, columns=["h3_id", "sez", "score", "approx_km"])
    if df.empty:
        st.info(
            "No matches. Run `dc ingest osm --layer sez` then `dc graph rebuild "
            "--only stakeholder` to populate SEZ nodes."
        )
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# Near hyperscaler / existing data center
# ---------------------------------------------------------------------------
with tabs[3]:
    # Find available companies from the graph.
    try:
        companies = query_rows(
            "MATCH (h:Hyperscaler) WHERE h.company IS NOT NULL "
            "RETURN DISTINCT h.company ORDER BY h.company"
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"Graph query failed: {exc}")
        companies = []
    options = [r[0] for r in companies if r[0]]
    if not options:
        st.info(
            "No hyperscaler nodes yet. Run `dc ingest osm --layer data-centers` "
            "then `dc graph rebuild --only stakeholder`."
        )
    else:
        company = st.selectbox("Company", options=options)
        max_km = st.slider("Max km", 5.0, 250.0, 100.0, 10.0)
        cypher, params = Q.hyperscaler_neighborhood(company, max_km=max_km)
        try:
            rows = query_rows(cypher, params)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Graph query failed: {exc}")
            rows = []
        df = pd.DataFrame(
            rows,
            columns=["h3_id", "state", "score", "data_center", "approx_km"],
        )
        st.dataframe(df.head(200), use_container_width=True, hide_index=True)

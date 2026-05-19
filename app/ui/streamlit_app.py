"""Entry point: `streamlit run app/ui/streamlit_app.py`."""
from __future__ import annotations

import streamlit as st

from app.ui._data import get_latest_run_id, list_scoring_runs

st.set_page_config(
    page_title="Sovereign AI Hotspots — India",
    layout="wide",
    initial_sidebar_state="expanded",
    page_icon="🛰",
)

# Dark base theme.
st.markdown(
    """
    <style>
      .stApp { background-color: #0b0f1a; color: #e6e9ef; }
      .stSidebar { background-color: #11162a; }
      h1, h2, h3 { color: #f5f7fb; }
      .stMetric { background: #11162a; border-radius: 10px; padding: 8px; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Sovereign AI Hotspots — Pan-India")
st.caption(
    "Tier-4 site selection across ~644k H3 res-7 hexagons. "
    "Live scoring against dual-feed power, water, fiber RoW, "
    "climate, solar, latency, and seismic/flood/protected-area exclusion."
)

runs = list_scoring_runs()
latest = get_latest_run_id()

c1, c2, c3, c4 = st.columns(4)
c1.metric("Latest score run", str(latest)[:8] if latest else "—")
c2.metric("Scoring runs (logged)", len(runs) if not runs.empty else 0)
c3.metric("Default resolution", "res 7 (~5 km²)")
c4.metric(
    "Cells scored (latest)",
    int(runs.iloc[0].cells_scored) if not runs.empty else 0,
)

st.divider()
st.markdown(
    "**Navigate:** use the sidebar to explore the Map, tune scoring weights, "
    "drill into a specific site, audit lineage, or compare two candidate cells."
)

st.info(
    "First time? Run, in order, from the project root:\n\n"
    "```bash\n"
    "make up && make init-db\n"
    "make build-grid && make push-grid-to-gee\n"
    "make ingest-all && make compute-features\n"
    "make score-default\n"
    "```",
    icon="ℹ️",
)

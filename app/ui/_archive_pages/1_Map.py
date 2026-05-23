"""Map page — Kepler.gl H3 layer w/ score choropleth + top-5 callouts."""
from __future__ import annotations

import pandas as pd
import pydeck as pdk
import streamlit as st

from app.core.db import session_scope
from app.ui._data import get_latest_run_id, load_top_sites

st.set_page_config(page_title="Map • DC Hotspots", layout="wide", page_icon="🗺")
st.title("🗺  Sovereign AI Hotspots Map")

run_id = get_latest_run_id()
if not run_id:
    st.warning("No scoring run found. Run `make score-default` first.")
    st.stop()

# State filter.
with session_scope() as session:
    from sqlalchemy import text
    state_rows = session.execute(
        text("SELECT DISTINCT state_code, state_name FROM dc_india.india_states ORDER BY state_name")
    ).all()
states_df = pd.DataFrame(state_rows, columns=["state_code", "state_name"])

c1, c2 = st.columns([2, 1])
with c1:
    state_choice = st.selectbox(
        "Filter by state",
        options=["(All India)"] + states_df.state_name.tolist(),
        index=0,
    )
with c2:
    top_n = st.slider("Top-N to highlight", 3, 20, 5)

state_code = (
    states_df.loc[states_df.state_name == state_choice, "state_code"].iloc[0]
    if state_choice != "(All India)"
    else None
)

# Pull res-7 scored cells (centroid + score). Heavy: cache.
@st.cache_data(ttl=300, show_spinner="Loading hex scores…")
def load_hex_scores(run_id: str, state_code: str | None = None) -> pd.DataFrame:
    from sqlalchemy import text

    sql = """
        SELECT s.h3_id::text AS h3_id,
               c.state_code,
               s.score,
               ST_X(ST_Centroid(g.geom)) AS lon,
               ST_Y(ST_Centroid(g.geom)) AS lat
        FROM dc_india.scores_res7 s
        JOIN dc_india.cell_features_res7 c ON c.h3_id = s.h3_id
        JOIN dc_india.h3_cells_res7      g ON g.h3_id = s.h3_id
        WHERE s.score_run_id = :run_id
          AND c.is_excluded = FALSE
    """
    params: dict = {"run_id": run_id}
    if state_code:
        sql += " AND c.state_code = :state"
        params["state"] = state_code

    with session_scope() as session:
        rows = session.execute(text(sql), params).all()
    return pd.DataFrame(rows, columns=["h3_id", "state_code", "score", "lon", "lat"])


hex_df = load_hex_scores(run_id, state_code)
if hex_df.empty:
    st.info("No scored, non-excluded cells in this scope.")
    st.stop()

# Top-N from top_sites table (diversity-respected).
top_df = load_top_sites(run_id)
if state_code:
    top_df = top_df[top_df.state_code == state_code]
top_df = top_df.sort_values("score", ascending=False).head(top_n)

st.caption(f"Showing **{len(hex_df):,}** scored cells. Highlighted top-{top_n} respect ≥50 km diversity.")

# Kepler.gl is heavyweight; use pydeck H3HexagonLayer (built-in) for the choropleth.
hex_layer = pdk.Layer(
    "H3HexagonLayer",
    data=hex_df.to_dict(orient="records"),
    pickable=True,
    stroked=False,
    filled=True,
    extruded=False,
    get_hexagon="h3_id",
    get_fill_color="[255*(1-score), 220*score, 80, 180]",
    opacity=0.7,
)
top_layer = pdk.Layer(
    "ScatterplotLayer",
    data=top_df.to_dict(orient="records"),
    pickable=True,
    get_position="[lon, lat]",
    get_fill_color=[255, 70, 70, 220],
    get_radius=5000,
    radius_min_pixels=8,
    radius_max_pixels=24,
)

view_state = pdk.ViewState(
    longitude=78.0,
    latitude=22.0,
    zoom=4 if state_code is None else 6,
    bearing=0,
    pitch=0,
)

deck = pdk.Deck(
    layers=[hex_layer, top_layer],
    initial_view_state=view_state,
    map_style="mapbox://styles/mapbox/dark-v10",
    tooltip={"text": "{state_code}\nscore: {score}"},
)

st.pydeck_chart(deck, use_container_width=True, height=720)

st.subheader(f"Top {top_n} sites" + (f" — {state_choice}" if state_choice != "(All India)" else ""))
display = top_df.copy()
display["score"] = display["score"].round(4)
st.dataframe(
    display[["rank", "state_code", "h3_id", "score", "lon", "lat"]],
    use_container_width=True,
    hide_index=True,
)

"""Grid resilience explorer — backed by the FalkorDB projection.

Pick a substation or a sub-grid; the page traverses the graph to find
every cell whose primary or dual-feed power depends on it, and plots
the impact on the map.

If FalkorDB is unreachable, the page shows a friendly fallback rather
than crashing the whole Streamlit app.
"""
from __future__ import annotations

import pandas as pd
import pydeck as pdk
import streamlit as st

from app.graph.client import health as graph_health
from app.graph.client import query_rows
from app.graph.queries import resilience as Q
from app.ui._data import get_latest_run_id

st.set_page_config(
    page_title="Resilience • DC Hotspots",
    layout="wide",
    page_icon="⚡",
)
st.title("⚡ Grid resilience explorer")
st.caption(
    "What happens to your candidate sites if one substation (or one sub-grid) "
    "goes down? The graph traverses CONNECTS / NEAREST_LINE / DUAL_FEED_LINE "
    "edges to compute impact."
)


# ---------------------------------------------------------------------------
# Graph health pre-flight. We want failures here to be obvious, not silent.
# ---------------------------------------------------------------------------
h = graph_health()
if not h.get("ok"):
    st.error(
        "FalkorDB is not reachable. Bring it up with `make graph-up` and seed "
        "the projection via `make graph-rebuild`."
    )
    st.json(h)
    st.stop()

st.success(
    f"FalkorDB connected — {h['graph']} @ {h['host']}:{h['port']} "
    f"({h['node_count']:,} nodes)"
)

score_run_id = get_latest_run_id()
st.caption(f"Active scoring run: `{score_run_id}`" if score_run_id else "No scoring runs yet")


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
tab1, tab2 = st.tabs(["Substation outage", "Sub-grid outage"])

with tab1:
    st.markdown(
        "Pick a substation by OSM id. The page returns every cell whose "
        "nearest HV line connects to it — these cells lose their primary feed."
    )
    # Surface candidate substations sorted by impact.
    systemic_cypher, _ = Q.systemic_substations(min_affected=1)
    try:
        sys_rows = query_rows(systemic_cypher, {"min_affected": 1})
    except Exception as exc:  # noqa: BLE001
        st.error(f"Graph query failed: {exc}")
        sys_rows = []

    sys_df = pd.DataFrame(
        sys_rows,
        columns=["osm_id", "name", "voltage_kv", "n_cells"],
    )

    if sys_df.empty:
        st.info("No substations in the graph yet. Run `make graph-rebuild`.")
        st.stop()

    sub_choice = st.selectbox(
        "Substation",
        options=sys_df.index,
        format_func=lambda i: (
            f"{sys_df.iloc[i]['name'] or '(unnamed)'}  •  "
            f"{sys_df.iloc[i]['voltage_kv']} kV  •  "
            f"impacts {sys_df.iloc[i]['n_cells']:,} cells"
        ),
    )
    chosen = sys_df.iloc[sub_choice]

    cypher, params = Q.cells_affected_by_substation_outage(
        osm_id=int(chosen.osm_id),
        score_run_id=score_run_id,
    )
    try:
        rows = query_rows(cypher, params)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Graph query failed: {exc}")
        rows = []

    affected = pd.DataFrame(
        rows, columns=["h3_id", "state_code", "lat", "lon", "line_km", "score"]
    )
    st.metric("Cells losing primary feed", f"{len(affected):,}")
    if not affected.empty:
        st.dataframe(
            affected.sort_values("score", ascending=False, na_position="last").head(50),
            use_container_width=True,
            hide_index=True,
        )

        # Map
        df_map = affected.dropna(subset=["lat", "lon"]).copy()
        df_map["radius"] = 5000
        st.pydeck_chart(
            pdk.Deck(
                initial_view_state=pdk.ViewState(
                    latitude=float(df_map.lat.mean()),
                    longitude=float(df_map.lon.mean()),
                    zoom=5,
                ),
                layers=[
                    pdk.Layer(
                        "ScatterplotLayer",
                        data=df_map,
                        get_position="[lon, lat]",
                        get_radius="radius",
                        get_fill_color="[230, 60, 60, 180]",
                        pickable=True,
                    )
                ],
                tooltip={
                    "html": "<b>{h3_id}</b><br/>line {line_km} km<br/>score {score}",
                    "style": {"color": "white"},
                },
            )
        )

with tab2:
    st.markdown(
        "Pick a sub-grid by ID. Cells whose **dual-feed** line lives in this "
        "sub-grid lose their topological redundancy if it fails."
    )
    sub_id_input = st.text_input("Sub-grid ID (integer)", value="")
    if not sub_id_input.strip().isdigit():
        st.info("Enter a numeric sub-grid ID. See available IDs with `dc graph query` and a MATCH on :SubGrid.")
    else:
        sub_id = int(sub_id_input)
        cypher, params = Q.cells_losing_dual_feed(sub_id)
        try:
            rows = query_rows(cypher, params)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Graph query failed: {exc}")
            rows = []
        losers = pd.DataFrame(
            rows,
            columns=["h3_id", "state_code", "lat", "lon", "dual_km"],
        )
        st.metric("Cells losing dual-feed redundancy", f"{len(losers):,}")
        if not losers.empty:
            st.dataframe(losers.head(50), use_container_width=True, hide_index=True)

"""Typer-based CLI. Entry point: `dc <command>` (see pyproject.toml).

Subcommands are wired here; implementations live in app/{grid,ingest,features,scoring}.
"""
from __future__ import annotations

import json
import sys

import typer

from app.core.logging import configure_logging, get_logger

configure_logging()
log = get_logger("dc")

app = typer.Typer(help="Pan-India AI Data Center site selection CLI.", no_args_is_help=True)
grid_app = typer.Typer(help="H3 grid management.", no_args_is_help=True)
ingest_app = typer.Typer(help="Data ingestion (GEE / OSM / WDPA / static).", no_args_is_help=True)
features_app = typer.Typer(help="Feature computation.", no_args_is_help=True)
app.add_typer(grid_app, name="grid")
app.add_typer(ingest_app, name="ingest")
app.add_typer(features_app, name="features")


# ----------------------------------------------------------------------------
# Top-level commands
# ----------------------------------------------------------------------------
@app.command("init-db")
def init_db() -> None:
    """Apply all migrations (idempotent)."""
    from app.core.db import apply_migrations, check_health

    health = check_health()
    if not health.get("ok"):
        typer.echo(f"DB unreachable: {health.get('error')}", err=True)
        raise typer.Exit(code=2)
    applied = apply_migrations()
    for name in applied:
        typer.echo(f"applied {name}")
    typer.echo("init-db ok")


@app.command("health")
def health() -> None:
    """Print DB connectivity + extension status."""
    from app.core.db import check_health

    typer.echo(json.dumps(check_health(), indent=2, default=str))


@app.command("validate")
def validate() -> None:
    """Run schema contracts + DQ checks across all sources."""
    from app.governance.validators import run_all_checks

    summary = run_all_checks()
    typer.echo(json.dumps(summary, indent=2, default=str))
    if summary.get("errors"):
        raise typer.Exit(code=1)


@app.command("serve")
def serve(port: int = 8501) -> None:
    """Launch the Streamlit UI."""
    import subprocess

    subprocess.run(
        [sys.executable, "-m", "streamlit", "run", "app/ui/streamlit_app.py", "--server.port", str(port)],
        check=False,
    )


# ----------------------------------------------------------------------------
# grid
# ----------------------------------------------------------------------------
@grid_app.command("build")
def grid_build(
    res: list[int] = typer.Option([6, 7, 8], "--res", help="Resolutions to build."),
) -> None:
    """Populate h3_cells_res{R} for each requested R from the GADM India boundary."""
    from app.grid.builder import build_grid

    for r in res:
        n = build_grid(r)
        typer.echo(f"res {r}: {n:,} cells")


@grid_app.command("push-to-gee")
def grid_push_to_gee(
    res: int = typer.Option(7, "--res"),
    chunk_size: int = typer.Option(5000, "--chunk-size"),
    wait: bool = typer.Option(
        True, "--wait/--no-wait", help="Block until every upload task completes."
    ),
) -> None:
    """Upload H3 cells (centroids + IDs) as a GEE FeatureCollection asset.

    Sub-assets are auto-merged at ingest time — no manual merge step needed.
    """
    from app.grid.gee_asset import push_cells_to_gee

    asset_id = push_cells_to_gee(resolution=res, chunk_size=chunk_size, wait=wait)
    typer.echo(f"asset uploaded: {asset_id}")


# ----------------------------------------------------------------------------
# ingest
# ----------------------------------------------------------------------------
@ingest_app.command("gee")
def ingest_gee(
    layer: str = typer.Option(..., help="seismic|flood|slope|landcover|solar|climate|population"),
    resolution: int = typer.Option(7, "--res"),
    fresh: bool = typer.Option(
        False,
        "--fresh",
        help="Bypass the skip-if-recent guard and re-run even if a recent success exists.",
    ),
) -> None:
    """Run GEE zonal-stats ingestion for one layer."""
    from app.ingest.gee import dispatch

    n = dispatch(layer=layer, resolution=resolution, fresh=fresh)
    typer.echo(f"gee.{layer} res={resolution}: {n:,} rows")


@ingest_app.command("gee-all")
def ingest_gee_all(
    resolution: int = typer.Option(7, "--res"),
    fresh: bool = typer.Option(False, "--fresh", help="Bypass skip-if-recent guard."),
    parallel_layers: int = typer.Option(
        1,
        "--parallel-layers",
        help=(
            "How many GEE layers to run concurrently. Each layer still uses its own "
            "chunk-level worker pool, so total in-flight requests = N × max_workers. "
            "Default 1. Push to 3 on free-tier GEE; higher on paid quotas."
        ),
    ),
) -> None:
    """Run EVERY GEE layer in a single process (avoids 7× subprocess startup).

    Per-layer skip-if-recent + per-chunk Parquet cache still apply.
    """
    from app.ingest.gee import ingest_all

    results = ingest_all(
        resolution=resolution, fresh=fresh, parallel_layers=parallel_layers
    )
    for layer, n in results.items():
        suffix = " (skipped or empty)" if n == 0 else ""
        typer.echo(f"gee.{layer} res={resolution}: {n:,} rows{suffix}")


@ingest_app.command("osm")
def ingest_osm(
    layer: str = typer.Option(..., help="power|highways|water|railways"),
    with_topology: bool = typer.Option(False, "--with-topology"),
    fresh: bool = typer.Option(False, "--fresh", help="Bypass skip-if-recent guard."),
) -> None:
    """Run OSM Overpass ingestion for one layer."""
    from app.ingest.osm import dispatch

    n = dispatch(layer=layer, with_topology=with_topology, fresh=fresh)
    typer.echo(f"osm.{layer}: {n:,} rows")


@ingest_app.command("wdpa")
def ingest_wdpa(
    india_only: bool = typer.Option(True, "--india-only/--no-india-only"),
    fresh: bool = typer.Option(False, "--fresh", help="Bypass skip-if-recent guard."),
) -> None:
    """Ingest WDPA protected areas via GEE."""
    from app.ingest.wdpa.protected_areas import ingest_wdpa as run

    n = run(india_only=india_only, fresh=fresh)
    typer.echo(f"wdpa: {n:,} rows")


@ingest_app.command("static")
def ingest_static(
    layer: str = typer.Option(..., help="cable-landings|metros"),
    fresh: bool = typer.Option(False, "--fresh", help="Bypass skip-if-recent guard."),
) -> None:
    """Load curated static datasets."""
    from app.ingest.static import dispatch

    n = dispatch(layer=layer, fresh=fresh)
    typer.echo(f"static.{layer}: {n:,} rows")


# ----------------------------------------------------------------------------
# features
# ----------------------------------------------------------------------------
@features_app.command("compute")
def features_compute(
    res: int = typer.Option(7, "--res"),
    kind: str = typer.Option("all", "--kind", help="exclusion|scoring|all"),
    top_n: int | None = typer.Option(None, "--top-n"),
) -> None:
    """Compute features for cells at the given resolution."""
    from app.features.build import compute_features

    n = compute_features(resolution=res, kind=kind, top_n=top_n)
    typer.echo(f"features res={res} kind={kind}: {n:,} cells")


# ----------------------------------------------------------------------------
# score
# ----------------------------------------------------------------------------
@app.command("score")
def score(
    weights: str | None = typer.Option(None, "--weights"),
    res: int = typer.Option(7, "--res"),
    top_n: int | None = typer.Option(None, "--top-n", help="Sites per state for diversity selection"),
) -> None:
    """Run scoring with the given weights file, then pick diversity-aware top-N."""
    from app.core.config import load_pipeline_config
    from app.scoring.algorithm import score_cells
    from app.scoring.ranking import select_top_sites

    cfg = load_pipeline_config()["scoring"]
    weights = weights or cfg.get("default_weights_file", "configs/weights/default.yml")

    run_id, n = score_cells(weights_path=weights, resolution=res)
    top = select_top_sites(
        run_id,
        n_per_state=top_n or int(cfg.get("top_n_per_state", 5)),
        min_km=float(cfg.get("diversity_min_km", 50.0)),
        resolution=res,
    )
    typer.echo(f"score_run_id={run_id} cells_scored={n:,} top_sites={top}")


if __name__ == "__main__":  # pragma: no cover
    app()

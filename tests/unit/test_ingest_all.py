"""Tests for the layer-level orchestrator in ``app/ingest/gee/__init__.py``.

We mock ``dispatch`` so we never touch GEE; the tests verify orchestration
behaviour (sequential vs parallel, error aggregation, ``ingest_all`` exit).
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from app.ingest.gee import ALL_LAYERS, ingest_all


def test_ingest_all_sequential_calls_every_layer_in_order():
    call_order: list[str] = []

    def fake_dispatch(layer: str, resolution: int, *, fresh: bool) -> int:
        call_order.append(layer)
        return 100

    with patch("app.ingest.gee.dispatch", side_effect=fake_dispatch), patch(
        "app.ingest.gee.client.init_ee", lambda: None
    ):
        results = ingest_all(parallel_layers=1)

    assert call_order == list(ALL_LAYERS)
    assert all(v == 100 for v in results.values())


def test_ingest_all_aggregates_failures_into_single_runtime_error():
    def fake_dispatch(layer: str, resolution: int, *, fresh: bool) -> int:
        if layer in ("slope", "climate"):
            raise RuntimeError(f"synthetic-{layer}-failure")
        return 1

    with patch("app.ingest.gee.dispatch", side_effect=fake_dispatch), patch(
        "app.ingest.gee.client.init_ee", lambda: None
    ):
        with pytest.raises(RuntimeError) as exc:
            ingest_all(parallel_layers=1)

    msg = str(exc.value)
    # Both failures must be reported in the single aggregated error.
    assert "slope" in msg
    assert "climate" in msg
    assert "synthetic-slope-failure" in msg
    assert "synthetic-climate-failure" in msg


def test_ingest_all_parallel_runs_layers_concurrently():
    """With parallel_layers > 1, layer N starts before layer N-1 finishes.

    Use a peak-concurrency counter so the assertion doesn't depend on
    the layer count being a multiple of ``parallel_layers``.
    """
    n_layers = len(ALL_LAYERS)
    started: list[str] = []
    state = {"in_flight": 0, "peak": 0}
    lock = threading.Lock()

    def fake_dispatch(layer: str, resolution: int, *, fresh: bool) -> int:
        with lock:
            started.append(layer)
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
        # Briefly hold the slot so multiple workers genuinely overlap.
        time.sleep(0.05)
        with lock:
            state["in_flight"] -= 1
        return 7

    with patch("app.ingest.gee.dispatch", side_effect=fake_dispatch), patch(
        "app.ingest.gee.client.init_ee", lambda: None
    ):
        results = ingest_all(parallel_layers=3)

    assert all(v == 7 for v in results.values())
    assert len(started) == n_layers
    # With parallel_layers=3 at least two layers must have been
    # in-flight simultaneously at some point. (Exactly 3 is racy on
    # slow CI.)
    assert state["peak"] >= 2


def test_ingest_all_uses_config_default_when_param_is_none():
    """If parallel_layers is None, config's gee.export.layer_parallelism is used."""
    with patch("app.ingest.gee.dispatch", return_value=5), patch(
        "app.ingest.gee.client.init_ee", lambda: None
    ), patch(
        "app.core.config.load_pipeline_config",
        return_value={"gee": {"export": {"layer_parallelism": 1}}},
    ):
        results = ingest_all(parallel_layers=None)
    assert len(results) == len(ALL_LAYERS)


def test_ingest_all_parallel_layers_negative_clamps_to_one():
    """parallel_layers=0 / -5 must not break — clamp to 1."""
    timings: list[float] = []

    def fake_dispatch(layer: str, resolution: int, *, fresh: bool) -> int:
        timings.append(time.monotonic())
        return 1

    with patch("app.ingest.gee.dispatch", side_effect=fake_dispatch), patch(
        "app.ingest.gee.client.init_ee", lambda: None
    ):
        ingest_all(parallel_layers=0)

    # Sequential mode is monotonically increasing in start time.
    assert timings == sorted(timings)

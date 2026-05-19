"""Score normalizers."""
from __future__ import annotations

import math

import numpy as np
import pytest

from app.scoring.transforms import (
    climate_score,
    exp_decay,
    latency_score,
    linear_clamp,
    power_redundancy_score,
    sigmoid,
)


def test_exp_decay_at_zero():
    assert exp_decay(0.0, decay_km=15.0) == pytest.approx(1.0)


def test_exp_decay_at_decay_constant():
    assert exp_decay(15.0, decay_km=15.0) == pytest.approx(math.e**-1, rel=1e-6)


def test_exp_decay_handles_nan():
    result = exp_decay(np.array([np.nan, 0.0, 30.0]), decay_km=15.0)
    assert result[0] == pytest.approx(0.0)
    assert result[1] == pytest.approx(1.0)
    assert result[2] == pytest.approx(math.e**-2, rel=1e-6)


def test_sigmoid_center_is_half():
    assert sigmoid(1400.0, center=1400.0, spread=200.0) == pytest.approx(0.5)


def test_linear_clamp_invert():
    assert linear_clamp(18, low=18, high=38, invert=True) == pytest.approx(1.0)
    assert linear_clamp(38, low=18, high=38, invert=True) == pytest.approx(0.0)
    assert linear_clamp(28, low=18, high=38, invert=True) == pytest.approx(0.5)


def test_climate_score_monotonic_decreasing():
    a = climate_score(mean_temp_c=20)
    b = climate_score(mean_temp_c=30)
    c = climate_score(mean_temp_c=40)
    assert a > b > c


def test_power_redundancy_distinct_grid_pulls_score_down():
    """Two nearby lines but only one sub-grid → score reflects 2nd-feed penalty."""
    same_grid = power_redundancy_score(nearest_km=1.0, nearest_distinct_subgrid_km=999.0)
    diff_grid = power_redundancy_score(nearest_km=1.0, nearest_distinct_subgrid_km=1.0)
    assert diff_grid > same_grid
    # Same-grid case should be roughly the primary leg only.
    assert same_grid == pytest.approx(0.6 * math.exp(-1.0 / 15.0), abs=1e-4)


def test_latency_decay_anchors():
    metro_only = latency_score(nearest_metro_km=0.0, nearest_cable_km=1e6)
    both = latency_score(nearest_metro_km=0.0, nearest_cable_km=0.0)
    assert both > metro_only

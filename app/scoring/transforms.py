"""Smooth normalizers that map raw feature values to ``[0, 1]``.

These are *the* place the algorithm encodes domain assumptions. Tweak with
care — see ``docs/tier4_methodology.md`` for justification of each curve.
"""
from __future__ import annotations

import numpy as np


def exp_decay(x: np.ndarray | float, *, decay_km: float) -> np.ndarray | float:
    """``exp(-x / decay_km)``. x=0 → 1.0, x=decay_km → ~0.37."""
    x = np.asarray(x, dtype=float)
    x = np.where(np.isnan(x), np.inf, x)
    return np.exp(-x / decay_km)


def sigmoid(x: np.ndarray | float, *, center: float, spread: float) -> np.ndarray | float:
    """Standard sigmoid centered at ``center``, scale ``spread``."""
    x = np.asarray(x, dtype=float)
    z = (x - center) / spread
    return 1.0 / (1.0 + np.exp(-z))


def linear_clamp(
    x: np.ndarray | float,
    *,
    low: float,
    high: float,
    invert: bool = False,
) -> np.ndarray | float:
    """Linear in [low, high], clamped to [0,1].

    invert=False: low→0, high→1.
    invert=True:  low→1, high→0 (used for "cooler = better" climate scoring).
    """
    x = np.asarray(x, dtype=float)
    raw = (x - low) / (high - low)
    raw = np.clip(raw, 0.0, 1.0)
    return 1.0 - raw if invert else raw


# Higher-level convenience builders used by the algorithm.
def power_redundancy_score(
    *,
    nearest_km: np.ndarray | float,
    nearest_distinct_subgrid_km: np.ndarray | float,
    primary_decay_km: float = 15.0,
    secondary_decay_km: float = 30.0,
    secondary_weight: float = 0.4,
) -> np.ndarray | float:
    """Composite: weighted combo of nearest-line + nearest-distinct-subgrid."""
    primary_weight = 1.0 - secondary_weight
    return (
        primary_weight * exp_decay(nearest_km, decay_km=primary_decay_km)
        + secondary_weight * exp_decay(nearest_distinct_subgrid_km, decay_km=secondary_decay_km)
    )


def latency_score(
    *,
    nearest_metro_km: np.ndarray | float,
    nearest_cable_km: np.ndarray | float,
    metro_decay_km: float = 100.0,
    cable_decay_km: float = 200.0,
    cable_weight: float = 0.5,
) -> np.ndarray | float:
    metro_w = 1.0 - cable_weight
    return (
        metro_w * exp_decay(nearest_metro_km, decay_km=metro_decay_km)
        + cable_weight * exp_decay(nearest_cable_km, decay_km=cable_decay_km)
    )


def climate_score(
    *,
    mean_temp_c: np.ndarray | float,
    temp_low_c: float = 18.0,
    temp_high_c: float = 38.0,
) -> np.ndarray | float:
    return linear_clamp(mean_temp_c, low=temp_low_c, high=temp_high_c, invert=True)

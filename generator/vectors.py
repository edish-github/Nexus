"""Placing playbook vectors relative to the world they were learned from.

A playbook's `precursor_embedding` is the pattern it claims to answer, so it
belongs near the centroid of the precursor snapshots for its archetype — near
enough to be retrieved as a candidate, far enough from its siblings that the
top-8 candidate list is a real field rather than eight copies of one point.

Placement is exact rather than approximate: given a unit centroid `c` and a
target cosine distance `d`, the returned vector has cosine similarity exactly
`1 - d` with `c`. That is what lets the seed guarantee that the two staged merge
pairs sit inside Chronicler's `distance < 0.15` predicate while every other
sibling pair sits outside it.
"""
from __future__ import annotations

from collections.abc import Sequence

import numpy as np


def unit(vector: Sequence[float]) -> np.ndarray:
    v = np.asarray(vector, dtype="float64")
    norm = np.linalg.norm(v)
    if norm == 0:
        raise ValueError("cannot normalize a zero vector")
    return v / norm


def centroid(vectors: Sequence[Sequence[float]]) -> np.ndarray:
    """The normalized mean of a set of embeddings."""
    if not vectors:
        raise ValueError("centroid of an empty set is undefined")
    return unit(np.mean(np.asarray(vectors, dtype="float64"), axis=0))


def place(anchor: Sequence[float], distance: float, rng: np.random.Generator) -> list[float]:
    """A unit vector at exactly `distance` cosine distance from `anchor`.

    Built by rotating the anchor towards a random direction in its orthogonal
    complement, so direction is arbitrary but the distance is precise.
    """
    if not 0.0 <= distance < 2.0:
        raise ValueError(f"cosine distance must be in [0, 2), got {distance}")
    c = unit(anchor)
    direction = rng.normal(size=c.shape)
    direction -= (direction @ c) * c  # project into the orthogonal complement
    norm = np.linalg.norm(direction)
    if norm == 0:  # pragma: no cover — astronomically unlikely
        return c.tolist()
    perpendicular = direction / norm
    cos_theta = 1.0 - distance
    sin_theta = float(np.sqrt(max(0.0, 1.0 - cos_theta**2)))
    return (c * cos_theta + perpendicular * sin_theta).tolist()

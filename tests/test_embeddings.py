"""The local embedder has to be a real embedding, not a hash with good PR.

The property that matters is ordering: windows of the same archetype must land
closer together than windows of different archetypes. If that fails, every
retrieval in the seeded world is noise, and it would still "work" in the sense
of returning rows.
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from generator import archetypes, world
from generator.trajectory import synthesize
from nexus_common import config, embeddings
from nexus_common.trajectory import trajectory_text


@pytest.fixture(autouse=True)
def _force_local_provider(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "local")
    embeddings.provider_name.cache_clear()
    yield
    embeddings.provider_name.cache_clear()


def _window_text(key: str, seed: int) -> str:
    arch = archetypes.get(key)
    traj = synthesize(
        arch, rng=np.random.default_rng(seed), service="payments", region="aws-us-east-1"
    )
    return trajectory_text(
        service="payments", region="aws-us-east-1",
        window_minutes=traj.precursor_minutes,
        metrics=traj.precursor_metrics(), phase="precursor",
    )


def test_provider_selection_is_explicit():
    assert embeddings.provider_name() == "local"


def test_dimension_matches_the_schema():
    assert len(embeddings.embed("telemetry window phase precursor")) == config.EMBEDDING_DIM


def test_embeddings_are_unit_length():
    v = embeddings.embed(_window_text("cache_stampede", 1))
    assert np.isclose(np.linalg.norm(v), 1.0)


def test_embedding_is_deterministic_across_calls():
    text = _window_text("disk_full", 2)
    assert embeddings.embed(text) == embeddings.embed(text)


def test_identical_text_embeds_identically_in_a_fresh_process_sense():
    """No process-local salt: the hash is keyed only by the term itself."""
    a = embeddings._hash_embed("metric pool_utilization trend rising", config.EMBEDDING_DIM)
    b = embeddings._hash_embed("metric pool_utilization trend rising", config.EMBEDDING_DIM)
    assert a == b


def test_same_archetype_is_closer_than_different_archetype():
    a1 = embeddings.embed(_window_text("connection_pool_exhaustion", 10))
    a2 = embeddings.embed(_window_text("connection_pool_exhaustion", 11))
    b = embeddings.embed(_window_text("dns_timeout_cascade", 12))
    assert embeddings.cosine(a1, a2) > embeddings.cosine(a1, b)


def test_every_archetype_is_self_consistent():
    """Within-archetype similarity beats the cross-archetype mean, for all eight."""
    vectors = {
        arch.key: [embeddings.embed(_window_text(arch.key, s)) for s in (21, 22, 23)]
        for arch in archetypes.ARCHETYPES
    }
    for key, group in vectors.items():
        within = embeddings.cosine(group[0], group[1])
        across = max(
            embeddings.cosine(group[0], other[0])
            for other_key, other in vectors.items()
            if other_key != key
        )
        assert within > across, f"{key} is not separable: within {within} <= across {across}"


def test_retrieval_over_the_seeded_world_is_accurate():
    """Leave-one-out 1-NN over the seeded precursor windows."""
    w = world.build()
    seeded = w.seeded_incidents[:80]
    vectors = np.array([embeddings.embed(i.window.precursor_text) for i in seeded])
    labels = [i.archetype for i in seeded]
    similarity = vectors @ vectors.T
    np.fill_diagonal(similarity, -1.0)
    correct = sum(
        labels[int(np.argmax(similarity[i]))] == labels[i] for i in range(len(seeded))
    )
    assert correct / len(seeded) >= 0.95


def test_to_vector_literal_round_trips_as_json():
    import json

    v = embeddings.embed("metric error_rate trend rising")
    assert len(json.loads(embeddings.to_vector_literal(v))) == config.EMBEDDING_DIM


def test_invalid_provider_is_rejected(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "titanic")
    embeddings.provider_name.cache_clear()
    with pytest.raises(ValueError, match="bedrock|local|auto"):
        embeddings.provider_name()


def test_auto_falls_back_when_no_credentials_resolve(monkeypatch):
    """`auto` must never fail closed: a machine with no AWS access still seeds."""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "auto")
    monkeypatch.setattr(
        "boto3.Session", lambda *a, **k: type("S", (), {"get_credentials": lambda self: None})()
    )
    embeddings.provider_name.cache_clear()
    assert embeddings.provider_name() == "local"
    assert os.environ["EMBEDDING_PROVIDER"] == "auto"

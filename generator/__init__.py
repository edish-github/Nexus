"""The synthetic world NEXUS remembers.

Two modes, one physics:

* **Historical** (`world.build()`) — 200 incidents and 60 negatives spread over
  90 simulated days, deterministic for a given seed, with a held-out backtest
  split that never touches the database. This is what `scripts/seed.py` writes.
* **Live** (`fleet.FleetSimulator`, served by `live.py`) — the same archetype
  curves driven forward in real time with a ramp control API, streaming into
  `telemetry_embeddings` through the ingestion path Oracle reads.

`archetypes` defines the eight failure patterns both modes are built from;
`playbooks` defines the seeded population and its family tree.
"""
from . import archetypes, playbooks, trajectory, vectors, world  # noqa: F401

__all__ = ["archetypes", "playbooks", "trajectory", "vectors", "world"]

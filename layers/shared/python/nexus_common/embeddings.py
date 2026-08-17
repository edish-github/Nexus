"""The single front door for turning text into a vector.

Two providers sit behind it:

* **`bedrock`** — Amazon Titan Text Embeddings V2, the production path. Every
  vector written by a deployed Lambda comes from here.
* **`local`** — a deterministic feature-hashing embedder with no network
  dependency, for seeding and testing a world on a machine with no AWS
  credentials. It is a real embedding (signed hashing trick over the canonical
  trajectory text's terms and bigrams, sub-linear term weighting, L2
  normalized), not random noise: two windows that serialize to similar text land
  close together, which is exactly the property the k-NN retrieval needs.

Choose with `EMBEDDING_PROVIDER=bedrock|local|auto` (default `auto`: Bedrock when
AWS credentials resolve, otherwise local with a warning).

**The two providers produce different vector spaces.** A database seeded with one
must be re-seeded before it is queried with the other, or every distance is
meaningless. `provider_name()` is recorded in the seed manifest so the mismatch
is detectable rather than silent.
"""
from __future__ import annotations

import functools
import hashlib
import os
import re

from . import config, log

logger = log.get_logger("embeddings")

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _resolve_provider() -> str:
    choice = os.environ.get("EMBEDDING_PROVIDER", "auto").strip().lower()
    if choice in ("bedrock", "local"):
        return choice
    if choice != "auto":
        raise ValueError(f"EMBEDDING_PROVIDER must be bedrock|local|auto, got {choice!r}")
    try:
        import boto3

        if boto3.Session().get_credentials() is not None:
            return "bedrock"
    except Exception as e:  # boto3 absent, or credential lookup blew up
        logger.warning("credential probe failed, falling back to local", error=str(e))
    logger.warning(
        "no AWS credentials resolved — using the local deterministic embedder; "
        "set EMBEDDING_PROVIDER=bedrock and re-seed before trusting production distances"
    )
    return "local"


@functools.cache
def provider_name() -> str:
    return _resolve_provider()


# --------------------------------------------------------------------------- #
# Local provider: signed feature hashing over the canonical text
# --------------------------------------------------------------------------- #

def _terms(text: str) -> list[str]:
    """Unigrams plus adjacent bigrams, so `trend rising` is its own feature."""
    words = _TOKEN_RE.findall(text.lower())
    return words + [f"{a}|{b}" for a, b in zip(words, words[1:], strict=False)]


def _hash_embed(text: str, dim: int) -> list[float]:
    import numpy as np

    vector = np.zeros(dim, dtype="float64")
    counts: dict[str, int] = {}
    for term in _terms(text):
        counts[term] = counts.get(term, 0) + 1
    for term, count in counts.items():
        digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
        raw = int.from_bytes(digest, "big")
        index = raw % dim
        sign = 1.0 if (raw >> 63) & 1 else -1.0
        vector[index] += sign * (1.0 + np.log(count))  # sub-linear term weighting
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:  # pragma: no cover — only for empty text
        raise ValueError("cannot embed text with no terms")
    return (vector / norm).tolist()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def embed(text: str) -> list[float]:
    """Embed one string with the active provider → EMBEDDING_DIM floats, L2-normalized."""
    if provider_name() == "bedrock":
        from . import bedrock

        return bedrock.embed(text)
    return _hash_embed(text, config.EMBEDDING_DIM)


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed many strings. Titan has no batch API, so this is a loop either way."""
    return [embed(t) for t in texts]


def to_vector_literal(vector: list[float]) -> str:
    """Render an embedding as a CockroachDB VECTOR literal: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity, for local assertions that don't want a database round trip."""
    import numpy as np

    u, v = np.asarray(a), np.asarray(b)
    denom = float(np.linalg.norm(u) * np.linalg.norm(v))
    return 0.0 if denom == 0.0 else float(u @ v / denom)

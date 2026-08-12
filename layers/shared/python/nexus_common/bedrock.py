"""Amazon Bedrock helpers: Titan Embeddings V2 + Claude reasoning.

Every call has bounded retry/backoff. Embeddings are L2-normalized and their
dimension is validated against config.EMBEDDING_DIM so a model/schema mismatch
raises immediately rather than corrupting the vector index.
"""
from __future__ import annotations

import functools
import json
import time
from typing import Any

from . import config, log

logger = log.get_logger("bedrock")


@functools.cache
def _client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "bedrock-runtime",
        region_name=config.AWS_REGION,
        config=Config(retries={"max_attempts": 3, "mode": "adaptive"}, read_timeout=60),
    )


def _invoke_with_retry(body: dict, model_id: str, attempts: int = 4) -> dict:
    delay = 0.5
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = _client().invoke_model(
                modelId=model_id,
                body=json.dumps(body),
                accept="application/json",
                contentType="application/json",
            )
            return json.loads(resp["body"].read())
        except Exception as e:  # botocore ClientError, ThrottlingException, timeouts
            last = e
            if attempt < attempts:
                logger.warning("bedrock retry", model=model_id, attempt=attempt, error=str(e))
                time.sleep(delay)
                delay = min(delay * 2, 8.0)
                continue
            raise
    raise last  # pragma: no cover


def embed(text: str, *, normalize: bool = True) -> list[float]:
    """Embed a single string with Titan Embeddings V2 → list[float] of EMBEDDING_DIM."""
    body = {"inputText": text, "dimensions": config.EMBEDDING_DIM, "normalize": normalize}
    out = _invoke_with_retry(body, config.BEDROCK_EMBEDDING_MODEL_ID)
    vector = out["embedding"]
    if len(vector) != config.EMBEDDING_DIM:
        raise ValueError(
            f"embedding dim {len(vector)} != configured {config.EMBEDDING_DIM} "
            f"(model {config.BEDROCK_EMBEDDING_MODEL_ID}) — schema/index would mismatch"
        )
    return vector


def embed_batch(texts: list[str], *, normalize: bool = True) -> list[list[float]]:
    """Embed many strings. Titan text-embed has no batch API, so this loops;
    kept as a single call site so callers don't hand-roll it."""
    return [embed(t, normalize=normalize) for t in texts]


def to_vector_literal(vector: list[float]) -> str:
    """Render an embedding as a CockroachDB VECTOR literal: '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.6f}" for x in vector) + "]"


def claude(prompt: str, *, system: str | None = None, max_tokens: int = 1024,
           temperature: float = 0.2) -> str:
    """Single-turn Claude call via the Bedrock Messages API → assistant text."""
    body: dict[str, Any] = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system
    out = _invoke_with_retry(body, config.BEDROCK_MODEL_ID)
    return "".join(block.get("text", "") for block in out.get("content", []))

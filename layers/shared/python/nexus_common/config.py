"""Configuration + secrets access, resolved once at Lambda cold start.

Secrets (DB connection string, CA cert, ccloud API key, changefeed shared
secret) live in AWS Secrets Manager. Non-secret config comes from environment
variables set by the SAM template. Nothing sensitive is ever read from the repo.
"""
from __future__ import annotations

import functools
import json
import os

# ---- Non-secret environment configuration (set by infra/template.yaml) -------
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")  # AWS region (not the CRDB 'aws-' label)
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "nexus-bus")
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")
ARTIFACTS_BUCKET = os.environ.get("ARTIFACTS_BUCKET", "")
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "NEXUS")

BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-opus-5")
BEDROCK_EMBEDDING_MODEL_ID = os.environ.get(
    "BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"
)
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))  # Titan V2 default

# Names of secrets in Secrets Manager (values fetched lazily, cached per cold start)
DB_SECRET_NAME = os.environ.get("DB_SECRET_NAME", "nexus/db")
CHANGEFEED_SECRET_NAME = os.environ.get("CHANGEFEED_SECRET_NAME", "nexus/changefeed")
CCLOUD_SECRET_NAME = os.environ.get("CCLOUD_SECRET_NAME", "nexus/ccloud")


@functools.cache
def _secrets_client():
    import boto3

    return boto3.client("secretsmanager", region_name=AWS_REGION)


@functools.cache
def get_secret(name: str) -> dict:
    """Fetch and cache a JSON secret from Secrets Manager for this cold start.

    Falls back to environment variables when running locally (no AWS creds),
    so `scripts/*` and unit tests work without Secrets Manager.
    """
    local = _local_secret_fallback(name)
    if local is not None:
        return local
    resp = _secrets_client().get_secret_value(SecretId=name)
    return json.loads(resp["SecretString"])


def _local_secret_fallback(name: str) -> dict | None:
    if name == DB_SECRET_NAME and os.environ.get("COCKROACH_DB_URL"):
        return {"dsn": os.environ["COCKROACH_DB_URL"]}
    if name == CHANGEFEED_SECRET_NAME and os.environ.get("CHANGEFEED_SHARED_SECRET"):
        return {"shared_secret": os.environ["CHANGEFEED_SHARED_SECRET"]}
    return None


def db_dsn() -> str:
    return get_secret(DB_SECRET_NAME)["dsn"]


def changefeed_shared_secret() -> str:
    return get_secret(CHANGEFEED_SECRET_NAME)["shared_secret"]

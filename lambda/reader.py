"""
reader.py  —  Reader Lambda (eu-central-1 replica region)
----------------------------------------------------------
Reads from the local replica by default.
Detects stale data using the written_at timestamp and version attribute.
Falls back to the primary region if:
  - The SSM read-routing flag is set to "primary"  (operator-driven failover)
  - The item is detectably stale beyond the staleness threshold
"""

import json
import logging
import os
import time
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME         = os.environ["TABLE_NAME"]
PRIMARY_REGION     = os.environ.get("PRIMARY_REGION", "us-east-1")
REPLICA_REGION     = os.environ.get("REPLICA_REGION", "eu-central-1")
READ_ROUTING_PARAM = os.environ.get("READ_ROUTING_PARAM", "/fis-global/read-routing")
CHAOS_SSM_PARAM    = os.environ.get("CHAOS_SSM_PARAM", "/fis-global/chaos-enabled")

# Only flag as stale if data is older than 10 seconds
STALENESS_THRESHOLD_MS = 10_000

ssm = boto3.client("ssm", region_name=PRIMARY_REGION)

ddb_primary = boto3.resource("dynamodb", region_name=PRIMARY_REGION)
ddb_replica  = boto3.resource("dynamodb", region_name=REPLICA_REGION)

table_primary = ddb_primary.Table(TABLE_NAME)
table_replica  = ddb_replica.Table(TABLE_NAME)

# Short-lived SSM cache — avoids a GetParameter call on every invocation
# but still picks up routing changes within 5 seconds
_ssm_cache: dict = {"value": None, "fetched_at": 0.0}
SSM_CACHE_TTL = 5  # seconds


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_read_routing() -> str:
    """
    Returns 'primary' or 'replica'.
    Caches the SSM value for SSM_CACHE_TTL seconds so a routing change
    (whether manual or from FIS) takes effect within one cache window.
    Defaults to 'replica' on any SSM error.
    """
    now = time.time()
    if _ssm_cache["value"] is not None and now - _ssm_cache["fetched_at"] < SSM_CACHE_TTL:
        return _ssm_cache["value"]
    try:
        resp = ssm.get_parameter(Name=READ_ROUTING_PARAM)
        value = resp["Parameter"]["Value"]
        _ssm_cache["value"] = value
        _ssm_cache["fetched_at"] = now
        logger.info("SSM routing refreshed: %s", value)
        return value
    except Exception as exc:
        logger.warning("SSM fetch failed, defaulting to replica: %s", exc)
        # Don't cache failures — try again next invocation
        _ssm_cache["value"] = None
        return "replica"


def is_stale(item: dict) -> tuple[bool, int]:
    """
    Returns (is_stale, age_ms).
    written_at is stored as a Decimal by DynamoDB — convert safely.
    Only flags stale if age exceeds STALENESS_THRESHOLD_MS.
    """
    written_at = item.get("written_at")
    if not written_at:
        return False, 0
    # DynamoDB returns numbers as Decimal — cast to int safely
    written_at_ms = int(Decimal(str(written_at)))
    now_ms = int(time.time() * 1000)
    age_ms = now_ms - written_at_ms
    stale = age_ms > STALENESS_THRESHOLD_MS
    return stale, age_ms


def read_item(table, pk: str, sk: str, consistent: bool = False):
    """Read a single item, optionally with strong consistency."""
    resp = table.get_item(
        Key={"pk": pk, "sk": sk},
        ConsistentRead=consistent,
    )
    return resp.get("Item")


def escalate_to_primary():
    """
    Write /fis-global/read-routing = 'primary' to SSM and update the
    local cache immediately.  Called the first time stale data is
    detected so that every subsequent invocation uses the ssm_flag
    path (🔀 SSM-REROUTED) rather than re-running per-item stale
    checks.  The rerouter Lambda resets it to 'replica' when the
    CloudWatch alarm clears.
    """
    try:
        ssm.put_parameter(
            Name=READ_ROUTING_PARAM,
            Value="primary",
            Type="String",
            Overwrite=True,
        )
        _ssm_cache["value"] = "primary"
        _ssm_cache["fetched_at"] = time.time()
        logger.warning("SSM escalated to 'primary' — stale replication detected")
    except Exception as exc:
        logger.warning("SSM escalation failed (will retry next invocation): %s", exc)


def serialize(item: dict) -> dict:
    out = {}
    for k, v in item.items():
        if isinstance(v, Decimal):
            out[k] = int(v) if v == v.to_integral_value() else float(v)
        else:
            out[k] = v
    return out


# ── Handler ──────────────────────────────────────────────────────────────────

def handler(event: dict, context) -> dict:
    logger.info("Reader invoked: %s", json.dumps(event))

    pk = event.get("pk")
    sk = event.get("sk", "default")

    if not pk:
        return {"statusCode": 400, "body": json.dumps({"error": "pk is required"})}

    routing = get_read_routing()
    stale_detected = False
    routing_reason = "normal"

    try:
        if routing == "primary":
            # Operator or FIS has forced reads to primary
            item = read_item(table_primary, pk, sk, consistent=True)
            read_from = "primary"
            routing_reason = "ssm_flag"

        else:
            # Default: read from replica
            item = read_item(table_replica, pk, sk, consistent=False)
            read_from = "replica"

            # Check for staleness only on replica reads
            if item:
                stale, age_ms = is_stale(item)
                if stale:
                    logger.warning(
                        "STALE DATA: pk=%s age=%dms — falling back to primary", pk, age_ms
                    )
                    item = read_item(table_primary, pk, sk, consistent=True)
                    read_from = "primary"
                    routing_reason = "stale_data"
                    stale_detected = True
                    # Self-escalate: flip SSM immediately so all subsequent reads
                    # skip per-item stale checks and use the ssm_flag path instead.
                    escalate_to_primary()
            else:
                # Item not found on replica — it may not have replicated yet
                # (common during FIS replication-pause experiments for new writes).
                # Try primary before giving up with a 404.
                primary_item = read_item(table_primary, pk, sk, consistent=True)
                if primary_item:
                    item = primary_item
                    read_from = "primary"
                    routing_reason = "replica_miss_fallback"
                    stale_detected = True
                    logger.warning(
                        "REPLICA MISS: pk=%s not on replica — fell back to primary", pk
                    )
                    escalate_to_primary()

        if item is None:
            return {
                "statusCode": 404,
                "body": json.dumps({"error": "item_not_found", "pk": pk, "sk": sk}),
            }

        _, age_ms = is_stale(item)

        logger.info(
            "Read OK pk=%s version=%s from=%s stale=%s reason=%s age=%dms",
            pk, item.get("version"), read_from, stale_detected, routing_reason, age_ms
        )

        return {
            "statusCode": 200,
            "body": json.dumps({
                "item": serialize(item),
                "read_from": read_from,
                "stale_detected": stale_detected,
                "routing_reason": routing_reason,
                "age_ms": age_ms,
            }),
        }

    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        logger.error("Read failed (%s): %s", read_from if "read_from" in dir() else "unknown", code)

        # Last resort: if replica threw an error, try primary
        if routing != "primary":
            try:
                item = read_item(table_primary, pk, sk, consistent=True)
                if item:
                    return {
                        "statusCode": 200,
                        "body": json.dumps({
                            "item": serialize(item),
                            "read_from": "primary",
                            "stale_detected": False,
                            "routing_reason": "replica_error_fallback",
                            "age_ms": 0,
                        }),
                    }
            except ClientError as exc2:
                logger.error("Primary fallback also failed: %s", exc2)

        return {
            "statusCode": 500,
            "body": json.dumps({"error": code}),
        }
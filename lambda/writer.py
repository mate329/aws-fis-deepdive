"""
writer.py  —  Writer Lambda (us-east-1 / primary region)
---------------------------------------------------------
Always writes to the primary DynamoDB endpoint.
Stamps every item with:
  - version   : monotonically incrementing integer (optimistic locking)
  - written_at: epoch seconds (stale-read detection baseline)
  - region    : which region wrote the item
"""

import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ["TABLE_NAME"]
# The writer always targets the primary region endpoint explicitly
PRIMARY_REGION = os.environ.get("AWS_REGION_OVERRIDE", "us-east-1")

ddb = boto3.resource("dynamodb", region_name=PRIMARY_REGION)
table = ddb.Table(TABLE_NAME)


def handler(event: dict, context) -> dict:
    """
    Expected event:
        { "pk": "user#123", "sk": "profile", "payload": { ... } }
    """
    logger.info("Writer invoked: %s", json.dumps(event))

    pk = event.get("pk", f"item#{int(time.time())}")
    sk = event.get("sk", "default")
    payload = event.get("payload", {})

    # ── Read current version (for optimistic locking) ──────────────────────
    current_version = 0
    try:
        existing = table.get_item(
            Key={"pk": pk, "sk": sk},
            ConsistentRead=True,          # writer always reads strongly consistent
        ).get("Item")
        if existing:
            current_version = int(existing.get("version", 0))
    except ClientError as exc:
        logger.warning("Could not read existing version: %s", exc)

    new_version = current_version + 1
    written_at = int(time.time() * 1000)  # milliseconds

    item = {
        "pk": pk,
        "sk": sk,
        "version": new_version,
        "written_at": written_at,
        "written_by_region": PRIMARY_REGION,
        **payload,
    }

    # ── Write with version condition (optimistic locking) ──────────────────
    try:
        if current_version == 0:
            # New item — must not exist yet
            table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(pk)",
            )
        else:
            # Update — only succeed if version hasn't changed (no concurrent write)
            table.put_item(
                Item=item,
                ConditionExpression="version = :expected_version",
                ExpressionAttributeValues={":expected_version": current_version},
            )

        logger.info("Write OK  pk=%s sk=%s version=%d", pk, sk, new_version)
        return {
            "statusCode": 200,
            "body": json.dumps({
                "pk": pk,
                "sk": sk,
                "version": new_version,
                "written_at": written_at,
                "region": PRIMARY_REGION,
            }),
        }

    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "ConditionalCheckFailedException":
            logger.warning("Optimistic lock conflict on pk=%s sk=%s", pk, sk)
            return {
                "statusCode": 409,
                "body": json.dumps({"error": "version_conflict", "pk": pk, "sk": sk}),
            }
        logger.error("Write failed: %s", code)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": code}),
        }

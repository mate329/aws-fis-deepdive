"""
ALB → Lambda integration: GET /health (Route 53 / ALB health checks) and GET /orders (DynamoDB scan).
"""

import json
import os
from decimal import Decimal

import boto3

_table = None


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):  # noqa: D102
        if isinstance(o, Decimal):
            return float(o) if o % 1 else int(o)
        return super().default(o)


def _table_resource():
    global _table  # noqa: PLW0603
    if _table is None:
        _ddb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION"))
        _table = _ddb.Table(os.environ["TABLE_NAME"])
    return _table


def _alb_response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "statusDescription": f"{status_code} OK" if status_code == 200 else f"{status_code}",
        "isBase64Encoded": False,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, cls=_DecimalEncoder),
    }


def handler(event, context):
    path = (event.get("path") or "").rstrip("/") or "/"
    if path == "/health":
        return _alb_response(
            200,
            {"status": "ok", "region": os.environ.get("AWS_REGION", "")},
        )
    if path == "/orders":
        try:
            resp = _table_resource().scan(Limit=50)
            items = resp.get("Items", [])
            return _alb_response(200, {"orders": items})
        except Exception as exc:  # noqa: BLE001
            return _alb_response(500, {"error": str(exc)})
    return _alb_response(404, {"error": "not_found", "path": path})

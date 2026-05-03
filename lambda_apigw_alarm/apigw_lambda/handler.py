"""
HTTP API Lambda handler — simple JSON echo used to exercise APIGW → Lambda → CW Errors.
When FIS injects aws:lambda:invocation-error, this handler does not run; API clients see 5xx.
"""

import json
import logging
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    # API Gateway HTTP API v2 payload format
    logger.info("Invocation ok request_id=%s", getattr(context, "aws_request_id", "?"))
    body = {
        "ok": True,
        "message": "hello from lambda",
        "function_name": os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""),
    }
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }

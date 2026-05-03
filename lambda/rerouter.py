"""
rerouter.py  —  Read-Routing Automation Lambda
-----------------------------------------------
Triggered by the ReplicationLatency CloudWatch alarm via SNS.

  ALARM  → sets /fis-global/read-routing = "primary"
           (forces all reads away from the stale eu-central-1 replica)

  OK     → sets /fis-global/read-routing = "replica"
           (restores normal replica reads once replication recovers)

This is the automation layer that the FIS experiments cannot provide
directly because aws:ssm:put-parameter is not a native FIS action.
"""

import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

READ_ROUTING_PARAM = os.environ["READ_ROUTING_PARAM"]
PRIMARY_REGION     = os.environ.get("PRIMARY_REGION", "us-east-1")

ssm = boto3.client("ssm", region_name=PRIMARY_REGION)


def handler(event, context):
    for record in event.get("Records", []):
        message = json.loads(record["Sns"]["Message"])
        alarm_state  = message.get("NewStateValue")
        alarm_name   = message.get("AlarmName", "unknown")
        alarm_reason = message.get("NewStateReason", "")

        logger.info(
            "Alarm notification: name=%s state=%s reason=%s",
            alarm_name, alarm_state, alarm_reason,
        )

        if alarm_state == "ALARM":
            new_value = "primary"
            logger.warning(
                "Replication latency alarm FIRING — rerouting reads to primary (%s)",
                PRIMARY_REGION,
            )
        elif alarm_state == "OK":
            new_value = "replica"
            logger.info(
                "Replication latency alarm RESOLVED — restoring reads to replica"
            )
        else:
            # INSUFFICIENT_DATA or unknown — leave routing unchanged
            logger.info("Ignoring alarm state '%s', no routing change", alarm_state)
            continue

        ssm.put_parameter(
            Name=READ_ROUTING_PARAM,
            Value=new_value,
            Type="String",
            Overwrite=True,
        )
        logger.info("SSM '%s' updated to '%s'", READ_ROUTING_PARAM, new_value)

    return {"statusCode": 200}

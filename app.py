#!/usr/bin/env python3
"""
app.py  —  CDK entrypoint for FIS demo stacks.

Deploy:
    cdk bootstrap aws://ACCOUNT/us-east-1
    cdk bootstrap aws://ACCOUNT/eu-central-1
    cdk deploy GlobalFisStack \
        --context account=123456789012
    cdk deploy LambdaApigwAlarmFisStack \
        --context account=123456789012

Multi-region Route 53 + FIS demo (see multi_region_lambdas/README.md):
    cdk bootstrap aws://ACCOUNT/us-east-1
    cdk bootstrap aws://ACCOUNT/eu-central-1
    cdk deploy OrdersGlobalStack OrdersRegionalUsEast1 OrdersRegionalEuCentral1
    # Account: omit --context account=... if the CDK CLI supplies CDK_DEFAULT_ACCOUNT (normal `cdk deploy`).
"""
import os

import aws_cdk as cdk
from global_fis_stack import GlobalFisStack
from lambda_apigw_alarm.lambda_apigw_alarm_stack import LambdaApigwAlarmFisStack
from multi_region_lambdas.orders_global_stack import OrdersGlobalStack
from multi_region_lambdas.orders_regional_stack import OrdersRegionalStack

app = cdk.App()

# Vpc.from_lookup needs a concrete account on the stack. CDK CLI sets CDK_DEFAULT_ACCOUNT on deploy/synth.
account = app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT")

GlobalFisStack(
    app,
    "GlobalFisStack",
    primary_region="us-east-1",
    replica_region="eu-central-1",
    env=cdk.Environment(account=account, region="us-east-1"),
    cross_region_references=True,
)

# HTTP API + Lambda + CW Errors alarm; FIS invocation-error (us-east-1 FIS layer ARN).
LambdaApigwAlarmFisStack(
    app,
    "LambdaApigwAlarmFisStack",
    env=cdk.Environment(account=account, region="us-east-1"),
)

# Route 53 failover (primary us-east-1, secondary eu-central-1) + ALB → Lambda + DDB global + FIS (us-east-1).
OrdersGlobalStack(
    app,
    "OrdersGlobalStack",
    env=cdk.Environment(account=account, region="us-east-1"),
    cross_region_references=True,
)
OrdersRegionalStack(
    app,
    "OrdersRegionalUsEast1",
    enable_fis_experiment=True,
    env=cdk.Environment(account=account, region="us-east-1"),
)
OrdersRegionalStack(
    app,
    "OrdersRegionalEuCentral1",
    attach_fis_extension=False,
    enable_fis_experiment=False,
    env=cdk.Environment(account=account, region="eu-central-1"),
)

app.synth()

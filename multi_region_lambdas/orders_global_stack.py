"""
Global stack (us-east-1): DynamoDB global table `orders` + optional Route 53 failover for
`api.orders.internal`, plus seed data. Pass ALB identifiers from regional stack outputs via
`cdk deploy -c` (see README).
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    RemovalPolicy,
    Tags,
    custom_resources as cr,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_route53 as route53,
)
from constructs import Construct

ORDERS_TABLE_NAME = "orders"
APP_TAG = "orders-demo"
ENV_TAG = "demo"

_SEED_ORDERS: list[dict[str, str]] = [
    {"orderId": "demo-1", "customer": "Ada", "total": "42.00", "sku": "BOOK-001"},
    {"orderId": "demo-2", "customer": "Grace", "total": "17.50", "sku": "MUG-002"},
    {"orderId": "demo-3", "customer": "Alan", "total": "128.99", "sku": "GPU-010"},
    {"orderId": "demo-4", "customer": "Barbara", "total": "9.00", "sku": "TEA-003"},
    {"orderId": "demo-5", "customer": "Edsger", "total": "55.55", "sku": "PEN-007"},
]

def _ddb_item_attr_map(row: dict[str, str]) -> dict[str, dict[str, str]]:
    return {k: {"S": v} for k, v in row.items()}


class OrdersGlobalStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Do not Tags.of(self) with app=orders-demo: stack tags propagate to AwsCustomResource
        # Lambdas (DynamoDB seed), and FIS tag-based targets would then include those functions.

        vpc = ec2.Vpc.from_lookup(self, "DefaultVpc", is_default=True)

        orders_table = dynamodb.TableV2(
            self,
            "OrdersGlobalTable",
            table_name=ORDERS_TABLE_NAME,
            partition_key=dynamodb.Attribute(
                name="orderId",
                type=dynamodb.AttributeType.STRING,
            ),
            billing=dynamodb.Billing.on_demand(),
            removal_policy=RemovalPolicy.DESTROY,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
            replicas=[
                dynamodb.ReplicaTableProps(
                    region="eu-central-1",
                ),
            ],
        )
        Tags.of(orders_table).add("app", APP_TAG)
        Tags.of(orders_table).add("environment", ENV_TAG)

        seed_policy = iam.PolicyStatement(
            actions=["dynamodb:PutItem"],
            resources=[orders_table.table_arn],
        )
        for i, row in enumerate(_SEED_ORDERS):
            seed = cr.AwsCustomResource(
                self,
                f"SeedOrder{i}",
                on_create=cr.AwsSdkCall(
                    service="DynamoDB",
                    action="putItem",
                    parameters={
                        "TableName": ORDERS_TABLE_NAME,
                        "Item": _ddb_item_attr_map(row),
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(
                        f"seed-{row['orderId']}",
                    ),
                ),
                policy=cr.AwsCustomResourcePolicy.from_statements([seed_policy]),
                install_latest_aws_sdk=False,
            )
            seed.node.add_dependency(orders_table)

        zone = route53.PrivateHostedZone(
            self,
            "OrdersInternalZone",
            zone_name="orders.internal",
            vpc=vpc,
        )
        Tags.of(zone).add("app", APP_TAG)
        Tags.of(zone).add("environment", ENV_TAG)

        primary_dns = self.node.try_get_context("ordersPrimaryAlbDns")
        primary_zone = self.node.try_get_context("ordersPrimaryAlbHostedZoneId")
        secondary_dns = self.node.try_get_context("ordersSecondaryAlbDns")
        secondary_zone = self.node.try_get_context("ordersSecondaryAlbHostedZoneId")

        if primary_dns and primary_zone and secondary_dns and secondary_zone:
            record_name = f"api.{zone.zone_name}"

            primary_hc = route53.CfnHealthCheck(
                self,
                "PrimaryUsEast1AlbHealthCheck",
                health_check_config=route53.CfnHealthCheck.HealthCheckConfigProperty(
                    type="HTTP",
                    fully_qualified_domain_name=primary_dns,
                    resource_path="/health",
                    port=80,
                    request_interval=30,
                    failure_threshold=3,
                ),
                health_check_tags=[
                    route53.CfnHealthCheck.HealthCheckTagProperty(
                        key="app",
                        value=APP_TAG,
                    ),
                    route53.CfnHealthCheck.HealthCheckTagProperty(
                        key="environment",
                        value=ENV_TAG,
                    ),
                ],
            )

            route53.CfnRecordSet(
                self,
                "ApiFailoverPrimary",
                hosted_zone_id=zone.hosted_zone_id,
                name=record_name,
                type="A",
                set_identifier="orders-primary-us-east-1",
                failover="PRIMARY",
                health_check_id=primary_hc.ref,
                alias_target=route53.CfnRecordSet.AliasTargetProperty(
                    dns_name=primary_dns,
                    hosted_zone_id=primary_zone,
                    evaluate_target_health=True,
                ),
            )

            route53.CfnRecordSet(
                self,
                "ApiFailoverSecondary",
                hosted_zone_id=zone.hosted_zone_id,
                name=record_name,
                type="A",
                set_identifier="orders-secondary-eu-central-1",
                failover="SECONDARY",
                alias_target=route53.CfnRecordSet.AliasTargetProperty(
                    dns_name=secondary_dns,
                    hosted_zone_id=secondary_zone,
                    evaluate_target_health=True,
                ),
            )

            cdk.CfnOutput(self, "Route53ApiFqdn", value=record_name)
            cdk.CfnOutput(self, "Route53HealthCheckId", value=primary_hc.ref)
        else:
            cdk.CfnOutput(
                self,
                "Route53Skipped",
                value=(
                    "Set context ordersPrimaryAlbDns, ordersPrimaryAlbHostedZoneId, "
                    "ordersSecondaryAlbDns, ordersSecondaryAlbHostedZoneId from regional outputs, "
                    "then redeploy this stack."
                ),
            )

        cdk.CfnOutput(self, "OrdersTableName", value=orders_table.table_name)
        cdk.CfnOutput(self, "PrivateHostedZoneId", value=zone.hosted_zone_id)
        cdk.CfnOutput(self, "PrivateHostedZoneName", value=zone.zone_name)

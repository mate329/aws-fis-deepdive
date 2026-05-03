"""
Regional stack (deploy once per region): default VPC, internet-facing ALB:80 → Lambda,
optional FIS extension (SSM) + S3 fault-config wiring, optional FIS experiment (primary only).

The managed extension SSM path is not available in every region; use attach_fis_extension=False
on secondary regions (for example eu-central-1) while keeping the same handler code.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Tags,
    aws_cloudwatch as cloudwatch,
    aws_ec2 as ec2,
    aws_elasticloadbalancingv2 as elbv2,
    aws_elasticloadbalancingv2_targets as elbv2_targets,
    aws_fis as fis,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_ssm as ssm,
)
from constructs import Construct

ORDERS_TABLE_NAME = "orders"
APP_TAG = "orders-demo"
ENV_TAG = "demo"

# Official name (see "Access Guide for Lambda Extension ARNs" in the FIS user guide). Older paths
# like /aws/service/fis/extension/lambda/layer/x86_64/latest are not valid for CloudFormation lookup.
DEFAULT_FIS_LAYER_SSM_NAME = (
    "/aws/service/fis/lambda-extension/AWS-FIS-extension-x86_64/1.x.x"
)


class OrdersRegionalStack(cdk.Stack):
    """
    :param attach_fis_extension: When True, resolve the FIS managed layer from SSM, create the
        fault-config S3 bucket, and wire env vars for the extension. Set False in regions where
        the FIS extension public parameter is not published (rare; depends on Region and FIS rollout).
    :param enable_fis_experiment: When True, creates the FIS template and stop alarm. Requires
        ``attach_fis_extension=True`` (FIS Lambda actions need the extension in that region).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        attach_fis_extension: bool = True,
        enable_fis_experiment: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if enable_fis_experiment and not attach_fis_extension:
            raise ValueError("enable_fis_experiment requires attach_fis_extension=True")

        # Avoid stack-level app=orders-demo: CDK can propagate stack tags to helper Lambdas,
        # which would break FIS target resolution (those functions lack AWS_FIS_CONFIGURATION_LOCATION).

        vpc = ec2.Vpc.from_lookup(self, "DefaultVpc", is_default=True)

        fis_config_bucket: s3.Bucket | None = None
        fis_config_prefix = "FisConfigs/"
        fis_layer_arn: str | None = None
        fis_config_location_arn: str | None = None

        if attach_fis_extension:
            fis_ssm_name = (
                self.node.try_get_context("ordersFisLayerSsmParameterName")
                or DEFAULT_FIS_LAYER_SSM_NAME
            )
            fis_layer_arn = ssm.StringParameter.value_for_string_parameter(
                self,
                fis_ssm_name,
            )
            fis_config_bucket = s3.Bucket(
                self,
                "FisLambdaConfigBucket",
                removal_policy=RemovalPolicy.DESTROY,
                auto_delete_objects=True,
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                encryption=s3.BucketEncryption.S3_MANAGED,
                enforce_ssl=True,
            )
            fis_config_location_arn = (
                f"arn:aws:s3:::{fis_config_bucket.bucket_name}/{fis_config_prefix}"
            )

        lambda_role = iam.Role(
            self,
            "OrdersApiRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        table_arn = (
            f"arn:aws:dynamodb:{self.region}:{self.account}:table/{ORDERS_TABLE_NAME}"
        )
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                sid="OrdersRead",
                actions=["dynamodb:Scan", "dynamodb:GetItem"],
                resources=[table_arn],
            )
        )
        if attach_fis_extension and fis_config_bucket is not None:
            lambda_role.add_to_policy(
                iam.PolicyStatement(
                    sid="AllowListingFisConfigLocation",
                    actions=["s3:ListBucket"],
                    resources=[fis_config_bucket.bucket_arn],
                    conditions={
                        "StringLike": {"s3:prefix": [f"{fis_config_prefix}*"]},
                    },
                )
            )
            lambda_role.add_to_policy(
                iam.PolicyStatement(
                    sid="AllowReadingFisConfigObjects",
                    actions=["s3:GetObject"],
                    resources=[f"{fis_config_bucket.bucket_arn}/{fis_config_prefix}*"],
                )
            )
            lambda_role.add_to_policy(
                iam.PolicyStatement(
                    sid="FisExtensionDescribeExperiments",
                    actions=["fis:GetExperiment", "fis:ListExperiments"],
                    resources=["*"],
                )
            )

        log_group = logs.LogGroup(
            self,
            "OrdersApiLogGroup",
            log_group_name=f"/aws/lambda/orders-api-{self.region}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        lambda_env: dict[str, str] = {"TABLE_NAME": ORDERS_TABLE_NAME}
        lambda_layers: list[lambda_.LayerVersion] = []
        if attach_fis_extension and fis_layer_arn and fis_config_location_arn:
            lambda_layers.append(
                lambda_.LayerVersion.from_layer_version_arn(
                    self,
                    "FisExtensionLayer",
                    fis_layer_arn,
                )
            )
            lambda_env["AWS_FIS_CONFIGURATION_LOCATION"] = fis_config_location_arn
            lambda_env["AWS_LAMBDA_EXEC_WRAPPER"] = "/opt/aws-fis/bootstrap"

        orders_fn = lambda_.Function(
            self,
            "OrdersApiFn",
            function_name=f"orders-api-{self.region}",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                "multi_region_lambdas/orders_lambda",
                exclude=[".venv", "venv", "__pycache__", "*.pyc"],
            ),
            role=lambda_role,
            timeout=Duration.seconds(15),
            memory_size=256,
            architecture=lambda_.Architecture.X86_64,
            layers=lambda_layers,
            environment=lambda_env,
            log_group=log_group,
        )
        Tags.of(orders_fn).add("app", APP_TAG)
        Tags.of(orders_fn).add("environment", ENV_TAG)

        alb = elbv2.ApplicationLoadBalancer(
            self,
            "OrdersAlb",
            vpc=vpc,
            internet_facing=True,
            load_balancer_name=f"orders-{self.region}".replace("-", "")[:32],
        )
        Tags.of(alb).add("app", APP_TAG)
        Tags.of(alb).add("environment", ENV_TAG)

        listener = alb.add_listener("Http", port=80, open=True)

        tg = elbv2.ApplicationTargetGroup(
            self,
            "OrdersLambdaTg",
            target_type=elbv2.TargetType.LAMBDA,
            targets=[elbv2_targets.LambdaTarget(orders_fn)],
            health_check=elbv2.HealthCheck(
                enabled=True,
                path="/health",
                healthy_http_codes="200",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=2,
            ),
        )
        Tags.of(tg).add("app", APP_TAG)
        Tags.of(tg).add("environment", ENV_TAG)

        listener.add_target_groups("ToOrdersLambda", target_groups=[tg])

        stop_alarm: cloudwatch.Alarm | None = None
        if enable_fis_experiment and fis_config_bucket is not None:
            stop_alarm = cloudwatch.Alarm(
                self,
                "OrdersLambdaErrorsStopAlarm",
                alarm_name=f"orders-lambda-errors-stop-{self.region}",
                alarm_description=(
                    "FIS stop condition: abort experiment if Lambda Errors sum exceeds 10 in "
                    "one minute (safety net)."
                ),
                metric=orders_fn.metric_errors(
                    statistic="Sum",
                    period=Duration.minutes(1),
                ),
                threshold=10,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )

            fis_role = iam.Role(
                self,
                "OrdersFisRole",
                assumed_by=iam.ServicePrincipal("fis.amazonaws.com"),
                description="FIS: Lambda invocation-error on orders API Lambda (by ARN)",
            )
            fis_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["tag:GetResources", "resource-groups:ListGroupResources"],
                    resources=["*"],
                )
            )
            fis_role.add_to_policy(
                iam.PolicyStatement(
                    sid="FisFaultConfigObjects",
                    actions=["s3:PutObject", "s3:DeleteObject"],
                    resources=[f"{fis_config_bucket.bucket_arn}/{fis_config_prefix}*"],
                )
            )
            fis_role.add_to_policy(
                iam.PolicyStatement(
                    sid="FisInspectLambda",
                    actions=["lambda:GetFunction"],
                    resources=["*"],
                )
            )
            fis_role.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "logs:CreateLogDelivery",
                        "logs:PutLogEvents",
                        "logs:DescribeLogGroups",
                        "logs:DescribeResourcePolicies",
                    ],
                    resources=["*"],
                )
            )
            fis_role.add_to_policy(
                iam.PolicyStatement(
                    sid="StopConditionDescribeAlarm",
                    actions=["cloudwatch:DescribeAlarms"],
                    resources=[stop_alarm.alarm_arn],
                )
            )

            fis.CfnExperimentTemplate(
                self,
                "OrdersInvocationErrorExperiment",
                description=(
                    "Inject aws:lambda:invocation-error on 100% of invocations for the orders ALB "
                    f"API Lambda in {self.region} (1 minute). Target is this function ARN only so "
                    "other Lambdas in the account are never selected."
                ),
                role_arn=fis_role.role_arn,
                stop_conditions=[
                    fis.CfnExperimentTemplate.ExperimentTemplateStopConditionProperty(
                        source="aws:cloudwatch:alarm",
                        value=stop_alarm.alarm_arn,
                    ),
                ],
                targets={
                    "ordersLambdas": fis.CfnExperimentTemplate.ExperimentTemplateTargetProperty(
                        resource_type="aws:lambda:function",
                        selection_mode="ALL",
                        resource_arns=[orders_fn.function_arn],
                    )
                },
                actions={
                    "injectError": fis.CfnExperimentTemplate.ExperimentTemplateActionProperty(
                        action_id="aws:lambda:invocation-error",
                        description="Return error without running handler (100% for 1 min)",
                        parameters={
                            "duration": "PT1M",
                            "invocationPercentage": "100",
                            "preventExecution": "true",
                        },
                        targets={"Functions": "ordersLambdas"},
                    ),
                },
                tags={"Experiment": "OrdersDemo-InvocationError"},
            )

            cdk.CfnOutput(self, "FisRoleArn", value=fis_role.role_arn)
            cdk.CfnOutput(self, "FisStopAlarmName", value=stop_alarm.alarm_name)
            cdk.CfnOutput(self, "FisExperimentTag", value="OrdersDemo-InvocationError")

        sg_id = alb.connections.security_groups[0].security_group_id

        cdk.CfnOutput(self, "AlbDnsName", value=alb.load_balancer_dns_name)
        cdk.CfnOutput(self, "AlbCanonicalHostedZoneId", value=alb.load_balancer_canonical_hosted_zone_id)
        cdk.CfnOutput(self, "AlbArn", value=alb.load_balancer_arn)
        cdk.CfnOutput(self, "AlbSecurityGroupId", value=sg_id)
        cdk.CfnOutput(self, "OrdersLambdaName", value=orders_fn.function_name)
        cdk.CfnOutput(
            self,
            "FisConfigBucketName",
            value=fis_config_bucket.bucket_name if fis_config_bucket else "(none)",
        )

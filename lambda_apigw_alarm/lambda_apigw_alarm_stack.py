"""
lambda_apigw_alarm_stack.py
----------------------------
HTTP API → Lambda (with FIS extension) + CloudWatch alarm on Lambda Errors.

FIS experiment: aws:lambda:invocation-error — while active, invocations fail before the
handler runs, Errors metric increases, and the alarm should go to ALARM.
"""

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_cloudwatch as cloudwatch,
    aws_fis as fis,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
)
from typing import Optional

from constructs import Construct


# Same public layer as global_fis_stack; only valid in us-east-1 at this ARN revision.
FIS_EXTENSION_LAYER_ARN_US_EAST_1 = (
    "arn:aws:lambda:us-east-1:211125607513:layer:aws-fis-extension-x86_64:280"
)


class LambdaApigwAlarmFisStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        fis_layer_arn: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        layer_arn = fis_layer_arn or FIS_EXTENSION_LAYER_ARN_US_EAST_1

        fis_config_bucket = s3.Bucket(
            self,
            "FisConfigBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
        )
        fis_config_prefix = "FisConfigs/"
        fis_config_location_arn = (
            f"arn:aws:s3:::{fis_config_bucket.bucket_name}/{fis_config_prefix}"
        )

        lambda_role = iam.Role(
            self,
            "ApiLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
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
                actions=["fis:GetExperiment", "fis:ListExperiments"],
                resources=["*"],
            )
        )

        api_log_group = logs.LogGroup(
            self,
            "ApiLambdaLogGroup",
            log_group_name="/aws/lambda/fis-apigw-alarm-api",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        api_lambda = lambda_.Function(
            self,
            "ApiLambda",
            function_name="fis-apigw-alarm-api",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                "lambda_apigw_alarm/apigw_lambda",
                exclude=[".venv", "venv", "__pycache__", "*.pyc"],
            ),
            role=lambda_role,
            timeout=Duration.seconds(10),
            memory_size=128,
            architecture=lambda_.Architecture.X86_64,
            layers=[
                lambda_.LayerVersion.from_layer_version_arn(
                    self,
                    "FisExtensionLayer",
                    layer_arn,
                )
            ],
            environment={
                "AWS_FIS_CONFIGURATION_LOCATION": fis_config_location_arn,
                "AWS_LAMBDA_EXEC_WRAPPER": "/opt/aws-fis/bootstrap",
            },
            log_group=api_log_group,
        )

        http_api = apigwv2.HttpApi(
            self,
            "HttpApi",
            api_name="fis-apigw-alarm",
            description="Demo API for Lambda Errors alarm + FIS invocation-error",
            default_integration=apigwv2_integrations.HttpLambdaIntegration(
                "DefaultIntegration",
                api_lambda,
            ),
        )

        errors_alarm = cloudwatch.Alarm(
            self,
            "LambdaInvocationErrorsAlarm",
            alarm_name="fis-apigw-alarm-lambda-errors",
            alarm_description=(
                "Lambda Errors (Sum) ≥ 1 in 1 minute — fires when FIS injects invocation errors"
            ),
            metric=api_lambda.metric_errors(
                statistic="Sum",
                period=Duration.minutes(1),
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        fis_role = iam.Role(
            self,
            "FisRole",
            assumed_by=iam.ServicePrincipal("fis.amazonaws.com"),
            description="FIS role: Lambda invocation-error for APIGW alarm demo",
        )
        fis_role.add_to_policy(
            iam.PolicyStatement(
                actions=["tag:GetResources", "resource-groups:ListGroupResources"],
                resources=["*"],
            )
        )
        fis_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowFisToWriteAndDeleteFaultConfigurations",
                actions=["s3:PutObject", "s3:DeleteObject"],
                resources=[f"{fis_config_bucket.bucket_arn}/{fis_config_prefix}*"],
            )
        )
        fis_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowFisToInspectLambdaFunctions",
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

        fis.CfnExperimentTemplate(
            self,
            "ApigwAlarmInvocationErrorExperiment",
            description=(
                "Force API Lambda to error on 100% of invocations for 2 min "
                "(APIGW clients see failures; Lambda Errors alarm should fire)."
            ),
            role_arn=fis_role.role_arn,
            stop_conditions=[
                fis.CfnExperimentTemplate.ExperimentTemplateStopConditionProperty(
                    source="none"
                )
            ],
            targets={
                "apiFn": fis.CfnExperimentTemplate.ExperimentTemplateTargetProperty(
                    resource_type="aws:lambda:function",
                    resource_arns=[api_lambda.function_arn],
                    selection_mode="ALL",
                )
            },
            actions={
                "injectError": fis.CfnExperimentTemplate.ExperimentTemplateActionProperty(
                    action_id="aws:lambda:invocation-error",
                    description="Return error without executing handler (APIGW → 5xx)",
                    parameters={
                        "duration": "PT2M",
                        "invocationPercentage": "100",
                        "preventExecution": "true",
                    },
                    targets={"Functions": "apiFn"},
                ),
            },
            tags={"Experiment": "Lambda-ApigwAlarm-InvocationError"},
        )

        cdk.CfnOutput(self, "HttpApiUrl", value=http_api.api_endpoint)
        cdk.CfnOutput(self, "ApiLambdaName", value=api_lambda.function_name)
        cdk.CfnOutput(self, "LambdaErrorsAlarmName", value=errors_alarm.alarm_name)
        cdk.CfnOutput(self, "FisRoleArn", value=fis_role.role_arn)
        cdk.CfnOutput(
            self,
            "FisConfigBucketNameOut",
            value=fis_config_bucket.bucket_name,
            description="S3 bucket for FIS Lambda extension fault configuration",
        )
        cdk.CfnOutput(
            self,
            "FisExperimentTag",
            value="Lambda-ApigwAlarm-InvocationError",
            description="FIS template tag Experiment=... for start_experiment / console",
        )

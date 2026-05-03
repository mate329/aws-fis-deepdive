"""
global_fis_stack.py
-------------------
Deploys:
  - DynamoDB Global Table  (us-east-1 primary, eu-central-1 replica)
  - Lambda in us-east-1   (writer  — always writes to primary)
  - Lambda in eu-central-1 (reader  — reads from replica, detects staleness)
  - Lambda in us-east-1   (rerouter — triggered by CW alarm via SNS, updates SSM routing flag)
  - SSM parameters in both regions for chaos flag + region routing config
  - S3 bucket for FIS Lambda extension configuration distribution
  - FIS experiment template: pause Global Table replication for 3 minutes
    (reader self-escalates SSM on first stale detect; CW alarm resets on recovery)
  - FIS experiment template: inject 12 s startup delay into reader Lambda invocations
    (aws:lambda:invocation-add-delay — latency-induced apparent staleness)
  - FIS experiment template: force reader Lambda to error on every invocation
    (aws:lambda:invocation-error — exercises replica_error_fallback path)
"""

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    aws_dynamodb as dynamodb,
    aws_fis as fis,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subscriptions,
    aws_ssm as ssm,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_s3 as s3
)
from constructs import Construct


class GlobalFisStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        primary_region: str,
        replica_region: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.primary_region = primary_region
        self.replica_region = replica_region

        # ------------------------------------------------------------------ #
        # 1. DynamoDB Global Table                                             #
        #    Defined in the primary region; replicationRegions adds replica.   #
        # ------------------------------------------------------------------ #
        global_table = dynamodb.TableV2(
            self,
            "GlobalTable",
            table_name="fis-global-demo",
            partition_key=dynamodb.Attribute(
                name="pk",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="sk",
                type=dynamodb.AttributeType.STRING,
            ),
            billing=dynamodb.Billing.provisioned(
                read_capacity=dynamodb.Capacity.fixed(5),
                write_capacity=dynamodb.Capacity.autoscaled(max_capacity=10),
            ),
            removal_policy=RemovalPolicy.DESTROY,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
            ),
            replicas=[
                dynamodb.ReplicaTableProps(
                    region=replica_region,
                    read_capacity=dynamodb.Capacity.fixed(5),
                )
            ],
        )

        # ------------------------------------------------------------------ #
        # 2. S3 Bucket for FIS Lambda Extension Configuration Distribution    #
        #    Per the docs, FIS writes fault config here; the Lambda extension  #
        #    layer reads it before each invocation. Must be in the same region #
        #    as the experiment is started from.                                #
        #    Docs: https://docs.aws.amazon.com/fis/latest/userguide/use-lambda-actions.html
        # ------------------------------------------------------------------ #
        fis_config_bucket = s3.Bucket(
            self,
            "FisConfigBucket",
            bucket_name=f"fis-lambda-config-{self.account}-{primary_region}",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
        )

        # S3 key prefix FIS will use for fault configuration objects
        fis_config_prefix = "FisConfigs/"
        # Full S3 ARN format required by AWS_FIS_CONFIGURATION_LOCATION env var
        fis_config_location_arn = (
            f"arn:aws:s3:::{fis_config_bucket.bucket_name}/{fis_config_prefix}"
        )

        # ------------------------------------------------------------------ #
        # 3. SSM Parameters                                                    #
        # ------------------------------------------------------------------ #
        # Routing flag: "primary" forces all reads to us-east-1,
        # "replica" (default) reads from local eu-central-1 replica.
        read_routing_param = ssm.StringParameter(
            self,
            "ReadRoutingParam",
            parameter_name="/fis-global/read-routing",
            string_value="replica",   # default: read from local replica
            description="Set to 'primary' to force all reads to us-east-1 (failsafe)",
        )

        # Chaos flag (toggled by FIS ssm:put-parameter action)
        chaos_param = ssm.StringParameter(
            self,
            "ChaosParam",
            parameter_name="/fis-global/chaos-enabled",
            string_value="false",
            description="Toggled by FIS to inject in-Lambda staleness",
        )

        # ------------------------------------------------------------------ #
        # 4. Shared Lambda IAM role                                            #
        # ------------------------------------------------------------------ #
        lambda_role = iam.Role(
            self,
            "LambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        global_table.grant_read_write_data(lambda_role)
        read_routing_param.grant_read(lambda_role)
        read_routing_param.grant_write(lambda_role)   # rerouter Lambda writes this param
        chaos_param.grant_read(lambda_role)

        # grant_read_write_data only covers the primary ARN; the reader Lambda
        # also talks to the eu-central-1 replica which has a distinct ARN.
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                    "dynamodb:BatchGetItem",
                ],
                resources=[
                    f"arn:aws:dynamodb:{replica_region}:{self.account}:table/{global_table.table_name}",
                    f"arn:aws:dynamodb:{replica_region}:{self.account}:table/{global_table.table_name}/index/*",
                ],
            )
        )

        # The FIS Lambda extension layer (attached to reader Lambda) must be
        # able to ListBucket and GetObject from the FIS config S3 bucket.
        # Required by: https://docs.aws.amazon.com/fis/latest/userguide/use-lambda-actions.html#lambda-prerequisites
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowListingFisConfigLocation",
                actions=["s3:ListBucket"],
                resources=[fis_config_bucket.bucket_arn],
                conditions={
                    "StringLike": {
                        "s3:prefix": [f"{fis_config_prefix}*"]
                    }
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

        # The FIS extension running inside the reader Lambda needs to call back
        # to the FIS service to check active experiment parameters.
        lambda_role.add_to_policy(
            iam.PolicyStatement(
                actions=["fis:GetExperiment", "fis:ListExperiments"],
                resources=["*"],
            )
        )

        # ------------------------------------------------------------------ #
        # 5. Writer Lambda                                                     #
        # ------------------------------------------------------------------ #
        writer_log_group = logs.LogGroup(
            self,
            "WriterLogGroup",
            log_group_name="/aws/lambda/fis-global-writer",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        writer_lambda = lambda_.Function(
            self,
            "WriterLambda",
            function_name="fis-global-writer",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="writer.handler",
            code=lambda_.Code.from_asset("lambda", exclude=[".venv","venv","__pycache__","cdk.out","*.pyc","node_modules",".git"]),
            role=lambda_role,
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "TABLE_NAME": global_table.table_name,
                "AWS_REGION_OVERRIDE": primary_region,
            },
            log_group=writer_log_group,
        )

        # ------------------------------------------------------------------ #
        # 6. Reader Lambda                                                     #
        # ------------------------------------------------------------------ #
        reader_log_group = logs.LogGroup(
            self,
            "ReaderLogGroup",
            log_group_name="/aws/lambda/fis-global-reader",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        reader_lambda = lambda_.Function(
            self,
            "ReaderLambda",
            function_name="fis-global-reader",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="reader.handler",
            code=lambda_.Code.from_asset("lambda", exclude=[".venv","venv","__pycache__","cdk.out","*.pyc","node_modules",".git"]),
            role=lambda_role,
            # Timeout must exceed max FIS startup delay (12 000 ms) + execution time.
            timeout=Duration.seconds(30),
            memory_size=256,
            architecture=lambda_.Architecture.X86_64,
            layers=[
                lambda_.LayerVersion.from_layer_version_arn(
                    self,
                    "FisExtensionLayer",
                    # VERY IMPORTANT LINE BELOW - WRITE ABOUT THIS!!!!!
                    "arn:aws:lambda:us-east-1:211125607513:layer:aws-fis-extension-x86_64:280",
                )
            ],
            environment={
                "TABLE_NAME": global_table.table_name,
                "PRIMARY_REGION": primary_region,
                "REPLICA_REGION": replica_region,
                "READ_ROUTING_PARAM": read_routing_param.parameter_name,
                "CHAOS_SSM_PARAM": chaos_param.parameter_name,
                # FIS writes fault config to this S3 ARN; the extension layer
                # reads it before each invocation.
                "AWS_FIS_CONFIGURATION_LOCATION": fis_config_location_arn,
                # Required alongside the FIS extension layer to intercept invocations.
                "AWS_LAMBDA_EXEC_WRAPPER": "/opt/aws-fis/bootstrap",
            },
            log_group=reader_log_group,
        )
        # ------------------------------------------------------------------ #
        # 7. Rerouter Lambda  (triggered by CW alarm via SNS)                 #
        #    Sets /fis-global/read-routing to 'primary' on ALARM, 'replica'   #
        #    on OK — the automation layer the FIS ssm action cannot provide.  #
        # ------------------------------------------------------------------ #
        rerouter_log_group = logs.LogGroup(
            self,
            "RerouterLogGroup",
            log_group_name="/aws/lambda/fis-global-rerouter",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        rerouter_lambda = lambda_.Function(
            self,
            "RerouterLambda",
            function_name="fis-global-rerouter",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="rerouter.handler",
            code=lambda_.Code.from_asset(
                "lambda",
                exclude=[".venv","venv","__pycache__","cdk.out","*.pyc","node_modules",".git"],
            ),
            role=lambda_role,
            timeout=Duration.seconds(30),
            memory_size=128,
            environment={
                "READ_ROUTING_PARAM": read_routing_param.parameter_name,
                "PRIMARY_REGION": primary_region,
            },
            log_group=rerouter_log_group,
        )

        # ------------------------------------------------------------------ #
        # 8. FIS IAM role                                                      #
        # ------------------------------------------------------------------ #
        fis_role = iam.Role(
            self,
            "FisRole",
            assumed_by=iam.ServicePrincipal("fis.amazonaws.com"),
            description="FIS role: pause Global Table replication + Lambda fault injection",
        )

        # aws:dynamodb:global-table-pause-replication requires a broad set of
        # DynamoDB control-plane permissions.
        fis_role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:*"],
                resources=[
                    global_table.table_arn,
                    "*",
                ],
            )
        )

        # FIS needs resource-group permissions to resolve targets
        fis_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "tag:GetResources",
                    "resource-groups:ListGroupResources",
                ],
                resources=["*"],
            )
        )

        # SSM: toggle chaos flag and read-routing flag
        fis_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:PutParameter", "ssm:GetParameter"],
                resources=[
                    read_routing_param.parameter_arn,
                    chaos_param.parameter_arn,
                ],
            )
        )

        # FIS needs to write/delete fault config objects in the S3 bucket and
        # inspect the reader Lambda function config.
        # Required by: https://docs.aws.amazon.com/fis/latest/userguide/use-lambda-actions.html#lambda-prerequisites
        fis_role.add_to_policy(
            iam.PolicyStatement(
                sid="AllowFisToWriteAndDeleteFaultConfigurations",
                actions=[
                    "s3:PutObject",
                    "s3:DeleteObject",
                ],
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

        # FIS needs to be able to write experiment logs
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

        # ------------------------------------------------------------------ #
        # 9. FIS Experiment: Pause Replication + Reroute (3 minutes)          #
        # ------------------------------------------------------------------ #
        fis.CfnExperimentTemplate(
            self,
            "PauseAndRerouteExperiment",
            description=(
                "Pause replication for 3 min — reader self-escalates SSM to primary "
                "on first stale detect; CW alarm resets it on recovery"
            ),
            role_arn=fis_role.role_arn,
            stop_conditions=[
                fis.CfnExperimentTemplate.ExperimentTemplateStopConditionProperty(
                    source="none"
                )
            ],
            targets={
                "globalTable": fis.CfnExperimentTemplate.ExperimentTemplateTargetProperty(
                    resource_type="aws:dynamodb:global-table",
                    resource_arns=[global_table.table_arn],
                    selection_mode="ALL",
                )
            },
            actions={
                "pauseReplication": fis.CfnExperimentTemplate.ExperimentTemplateActionProperty(
                    action_id="aws:dynamodb:global-table-pause-replication",
                    description="Stop replication to replica for 3 minutes",
                    parameters={"duration": "PT3M"},
                    targets={"Tables": "globalTable"},
                ),
            },
            tags={"Experiment": "GlobalTable-PauseAndReroute"},
        )

        # ------------------------------------------------------------------ #
        # 10. FIS Experiment: Lambda Invocation Delay (2 minutes)             #
        #    Injects a 12 s startup delay into 100 % of reader Lambda calls.  #
        #    Requires the FIS extension layer on the reader Lambda and the     #
        #    S3 config bucket wired up via AWS_FIS_CONFIGURATION_LOCATION.    #
        # ------------------------------------------------------------------ #
        fis.CfnExperimentTemplate(
            self,
            "InvocationDelayExperiment",
            description=(
                "Inject 12 s startup delay into 100% of reader Lambda invocations for 2 min. "
                "Delay > STALENESS_THRESHOLD_MS → stale-data fallback fires even with healthy replication."
            ),
            role_arn=fis_role.role_arn,
            stop_conditions=[
                fis.CfnExperimentTemplate.ExperimentTemplateStopConditionProperty(
                    source="none"
                )
            ],
            targets={
                "readerFn": fis.CfnExperimentTemplate.ExperimentTemplateTargetProperty(
                    resource_type="aws:lambda:function",
                    resource_arns=[reader_lambda.function_arn],
                    selection_mode="ALL",
                )
            },
            actions={
                "addDelay": fis.CfnExperimentTemplate.ExperimentTemplateActionProperty(
                    action_id="aws:lambda:invocation-add-delay",
                    description="Add 12 s pre-handler delay to every reader invocation",
                    parameters={
                        "duration": "PT2M",
                        "invocationPercentage": "100",
                        "startupDelayMilliseconds": "12000",
                    },
                    targets={"Functions": "readerFn"},
                ),
            },
            tags={"Experiment": "Lambda-InvocationDelay"},
        )

        # ------------------------------------------------------------------ #
        # 11. FIS Experiment: Lambda Invocation Error (2 minutes)             #
        #    Forces reader Lambda to return an error response on 100 % of     #
        #    invocations without executing the handler.                        #
        # ------------------------------------------------------------------ #
        fis.CfnExperimentTemplate(
            self,
            "InvocationErrorExperiment",
            description=(
                "Force reader Lambda to return an error on 100% of invocations for 2 min. "
                "Triggers replica_error_fallback path in reader."
            ),
            role_arn=fis_role.role_arn,
            stop_conditions=[
                fis.CfnExperimentTemplate.ExperimentTemplateStopConditionProperty(
                    source="none"
                )
            ],
            targets={
                "readerFn": fis.CfnExperimentTemplate.ExperimentTemplateTargetProperty(
                    resource_type="aws:lambda:function",
                    resource_arns=[reader_lambda.function_arn],
                    selection_mode="ALL",
                )
            },
            actions={
                "injectError": fis.CfnExperimentTemplate.ExperimentTemplateActionProperty(
                    action_id="aws:lambda:invocation-error",
                    description="Return error response without executing reader handler",
                    parameters={
                        "duration": "PT2M",
                        "invocationPercentage": "100",
                        "preventExecution": "true",
                    },
                    targets={"Functions": "readerFn"},
                ),
            },
            tags={"Experiment": "Lambda-InvocationError"},
        )

        # ------------------------------------------------------------------ #
        # 12. CloudWatch alarm: ReplicationLatency > 5 seconds                #
        #     ALARM fires → SNS → rerouter Lambda sets SSM to 'primary'       #
        #     OK fires    → SNS → rerouter Lambda sets SSM back to 'replica'  #
        # ------------------------------------------------------------------ #
        replication_alarm_topic = sns.Topic(
            self,
            "ReplicationAlarmTopic",
            topic_name="fis-global-replication-alarm",
            display_name="DynamoDB Global Table ReplicationLatency alarm",
        )
        replication_alarm_topic.add_subscription(
            sns_subscriptions.LambdaSubscription(rerouter_lambda)
        )

        replication_alarm = cloudwatch.Alarm(
            self,
            "ReplicationLatencyAlarm",
            alarm_name="DDB-GlobalTable-ReplicationLatency",
            alarm_description="Replication lag from us-east-1 to eu-central-1 exceeds 5 seconds",
            metric=cloudwatch.Metric(
                namespace="AWS/DynamoDB",
                metric_name="ReplicationLatency",
                dimensions_map={
                    "TableName": global_table.table_name,
                    "ReceivingRegion": replica_region,
                },
                statistic="Maximum",
                period=Duration.seconds(60),
            ),
            threshold=5000,           # milliseconds
            evaluation_periods=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        replication_alarm.add_alarm_action(cw_actions.SnsAction(replication_alarm_topic))
        replication_alarm.add_ok_action(cw_actions.SnsAction(replication_alarm_topic))

        # ------------------------------------------------------------------ #
        # 13. Outputs                                                          #
        # ------------------------------------------------------------------ #
        cdk.CfnOutput(self, "OutputTableName", value=global_table.table_name)
        cdk.CfnOutput(self, "OutputWriterLambda", value=writer_lambda.function_name)
        cdk.CfnOutput(self, "OutputReaderLambda", value=reader_lambda.function_name)
        cdk.CfnOutput(self, "OutputRerouterLambda", value=rerouter_lambda.function_name)
        cdk.CfnOutput(self, "OutputReadRoutingParam", value=read_routing_param.parameter_name)
        cdk.CfnOutput(self, "OutputChaosParam", value=chaos_param.parameter_name)
        cdk.CfnOutput(self, "OutputFisRoleArn", value=fis_role.role_arn)
        cdk.CfnOutput(
            self,
            "OutputFisConfigBucket",
            value=fis_config_bucket.bucket_name,
            description="S3 bucket used by the FIS Lambda extension for fault configuration distribution",
        )
        cdk.CfnOutput(
            self,
            "OutputFisConfigLocationArn",
            value=fis_config_location_arn,
            description="Value set in AWS_FIS_CONFIGURATION_LOCATION on the reader Lambda",
        )
        cdk.CfnOutput(
            self,
            "PrimaryTableArn",
            value=global_table.table_arn,
            description="Use this ARN in the FIS console to start the experiment",
        )
        cdk.CfnOutput(
            self,
            "FisExperimentTagGlobalTable",
            value="GlobalTable-PauseAndReroute",
            description="Tag value for the DynamoDB replication-pause FIS experiment",
        )
        cdk.CfnOutput(
            self,
            "FisExperimentTagLambdaDelay",
            value="Lambda-InvocationDelay",
            description="Tag value for the Lambda invocation-delay FIS experiment",
        )
        cdk.CfnOutput(
            self,
            "FisExperimentTagLambdaError",
            value="Lambda-InvocationError",
            description="Tag value for the Lambda invocation-error FIS experiment",
        )
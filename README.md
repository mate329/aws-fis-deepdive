# aws-fis-deepdive

AWS CDK (Python) examples for [**AWS Fault Injection Service (FIS)**](https://docs.aws.amazon.com/fis/) alongside real workloads: DynamoDB global tables, Lambda with the FIS managed extension, API Gateway HTTP APIs, CloudWatch alarms, and a multi-Region Route 53 failover path to ALBs.

The CDK app is defined in `app.py`. Synth and deploy use `cdk.json` → `python3 app.py`.

## Prerequisites

- Python **3.12+** (Lambda runtimes in the stacks target 3.12)
- [AWS CDK CLI](https://docs.aws.amazon.com/cdk/v2/guide/getting_started.html): `npm i -g aws-cdk`
- AWS credentials for the target account
- Install Python deps from the repo root:

  ```bash
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  pip install boto3
  ```

  `boto3` is only required for the `test_*.py` client scripts, not for `cdk synth` / `cdk deploy`.

## Bootstrap

Stacks use **us-east-1** and **eu-central-1**. Bootstrap both (replace `ACCOUNT`):

```bash
cdk bootstrap aws://ACCOUNT/us-east-1
cdk bootstrap aws://ACCOUNT/eu-central-1
```

## Stacks (from `app.py`)

| Stack | Region | Summary |
|--------|--------|---------|
| `GlobalFisStack` | `us-east-1` (cross-region refs) | Global table `fis-global-demo`, Lambdas `fis-global-writer` / `fis-global-reader` / `fis-global-rerouter`, SSM routing + chaos flags, S3 for FIS Lambda extension config, replication alarm → SNS → rerouter, FIS templates (global table pause, Lambda delay/error) |
| `LambdaApigwAlarmFisStack` | `us-east-1` | HTTP API → Lambda, CloudWatch alarm on Lambda errors, FIS invocation-error experiment |
| `OrdersGlobalStack` | `us-east-1` | Global orders table, DNS zone, optional failover alias (context-driven) |
| `OrdersRegionalUsEast1` | `us-east-1` | ALB → orders Lambda, FIS extension + experiment |
| `OrdersRegionalEuCentral1` | `eu-central-1` | Same pattern without FIS on secondary (configurable in `app.py`) |

Deploy one or more stacks, for example:

```bash
cdk deploy GlobalFisStack
cdk deploy LambdaApigwAlarmFisStack
cdk deploy OrdersGlobalStack OrdersRegionalUsEast1 OrdersRegionalEuCentral1
```

**Account ID:** `Vpc.from_lookup` and stack env need a concrete account. The CLI usually sets `CDK_DEFAULT_ACCOUNT` when you deploy locally. Otherwise pass `--context account=ACCOUNT_ID` (see the docstring at the top of `app.py`).

## Client scripts (`test_*.py`)

These are **not** pytest suites; they are CLI tools that call AWS APIs (Lambda, FIS, CloudFormation, HTTP) against resources created by CDK.

| Script | Needs | Purpose |
|--------|--------|---------|
| [`test_global_chaos.py`](test_global_chaos.py) | `GlobalFisStack` | Baseline / drift / FIS delay / FIS error scenarios; optional `--watch` loop |
| [`test_lambda_apigw_alarm.py`](test_lambda_apigw_alarm.py) | `LambdaApigwAlarmFisStack` | Hit HTTP API, start/stop FIS, observe alarm |
| [`test_multi_region_orders.py`](test_multi_region_orders.py) | Orders stacks | ALB checks, optional FIS, optional Route 53, optional unit tests |

Examples:

```bash
python test_global_chaos.py --scenario baseline
python test_global_chaos.py --watch

python test_lambda_apigw_alarm.py

python test_multi_region_orders.py
python test_multi_region_orders.py --with-fis --try-route53
```

## Multi-region orders detail

Deploy order, Route 53 context keys, FIS extension SSM parameter path, and troubleshooting are documented in [**multi_region_lambdas/README.md**](multi_region_lambdas/README.md).

## FIS Lambda extension ARNs

`GlobalFisStack` and `LambdaApigwAlarmFisStack` reference a **pinned managed extension layer ARN** in `us-east-1`. If layer deploy fails, update the ARN in `global_fis_stack.py` and `lambda_apigw_alarm/lambda_apigw_alarm_stack.py` to match current AWS documentation for your Region.

## Layout

```
app.py                    # CDK app entry
global_fis_stack.py       # Global table + global FIS demo
lambda/                   # writer, reader, rerouter (GlobalFisStack asset)
lambda_apigw_alarm/       # HTTP API + alarm stack
multi_region_lambdas/     # Orders + Route 53 failover demo
requirements.txt          # CDK constructs (add boto3 for test scripts)
test_global_chaos.py
test_lambda_apigw_alarm.py
test_multi_region_orders.py
```

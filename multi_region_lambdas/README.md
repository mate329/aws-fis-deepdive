# Multi-region orders demo (Route 53 failover + FIS)

This folder defines a small **Route 53 failover → ALB → Lambda → DynamoDB global table** setup across **us-east-1** (primary) and **eu-central-1** (secondary), plus an **AWS FIS** experiment template in **us-east-1** that injects `aws:lambda:invocation-error` into the **orders API Lambda only** (targeted by ARN so other Lambdas in the region are never selected).

Stacks:

| Stack | Region | Purpose |
|--------|--------|---------|
| `OrdersGlobalStack` | `us-east-1` | DynamoDB global table `orders` (replica in `eu-central-1`), seed data, private hosted zone `orders.internal`, failover alias `api.orders.internal` when context is set |
| `OrdersRegionalUsEast1` | `us-east-1` | ALB, orders Lambda, FIS extension layer (SSM), FIS experiment + stop alarm |
| `OrdersRegionalEuCentral1` | `eu-central-1` | Same API without the FIS Lambda extension (the managed extension SSM parameter is not available in every region, including many secondary-region choices) and without the FIS template |

## Prerequisites

- Python 3 with a venv; install deps: `pip install -r requirements.txt`
- AWS CDK CLI (`npm i -g aws-cdk`)
- **Active AWS credentials** for the target account (regional stacks call `Vpc.from_lookup` for the **default VPC**; CDK needs `ec2:DescribeVpcs` once so `cdk synth` / `cdk deploy` can resolve VPC and subnet IDs).
- CDK bootstrapping in **both** regions (replace `ACCOUNT`):

```bash
cdk bootstrap aws://ACCOUNT/us-east-1
cdk bootstrap aws://ACCOUNT/eu-central-1
```

## Deploy order

1. **Global data + DNS zone (no failover records yet)**  
   Deploy the global stack once **without** the optional Route 53 context keys (failover records are omitted until you pass all four values on a later deploy):

```bash
cdk deploy OrdersGlobalStack
```

Use `--context account=ACCOUNT` only if the CDK process does not receive `CDK_DEFAULT_ACCOUNT` (for example some CI jobs). A normal local `cdk deploy` with AWS credentials resolves the account automatically.

2. **Both regional stacks**

```bash
cdk deploy OrdersRegionalUsEast1 OrdersRegionalEuCentral1
```

3. **Wire Route 53 to the ALBs**  
   From the CloudFormation outputs of each regional stack, copy **AlbDnsName** and **AlbCanonicalHostedZoneId** (Route 53 alias target needs the ALB hosted zone ID, not a security group). Redeploy the global stack with:

```bash
cdk deploy OrdersGlobalStack \
  --context ordersPrimaryAlbDns=PRIMARY_ALB_DNS \
  --context ordersPrimaryAlbHostedZoneId=PRIMARY_ALB_HOSTED_ZONE_ID \
  --context ordersSecondaryAlbDns=SECONDARY_ALB_DNS \
  --context ordersSecondaryAlbHostedZoneId=SECONDARY_ALB_HOSTED_ZONE_ID
```

Use **us-east-1** regional outputs for the **primary** context keys and **eu-central-1** outputs for the **secondary** keys.

> **Stack parameters:** CDK resolves these as **synthesis context** (`-c` / `cdk.context.json`). That matches the usual workflow: deploy regionals, copy outputs, redeploy global with the four strings.

## Attach the FIS managed Lambda extension (before running the experiment)

**Primary (`OrdersRegionalUsEast1`)** attaches the **managed FIS Lambda extension** using the **SSM public parameter** (in that region), per the current FIS docs:

`/aws/service/fis/lambda-extension/AWS-FIS-extension-x86_64/1.x.x`

(Older blog snippets may reference `/aws/service/fis/extension/lambda/layer/x86_64/latest`; that path is **not** what Parameter Store exposes today, and deploy will fail with “Unable to fetch parameters”.)

and sets:

- `AWS_FIS_CONFIGURATION_LOCATION` → `s3://<bucket>/FisConfigs/` (same region as the Lambda)
- `AWS_LAMBDA_EXEC_WRAPPER` → `/opt/aws-fis/bootstrap`

The **secondary** stack in `eu-central-1` uses **`attach_fis_extension=False`** so you are not blocked if that Region still lacks the public parameter. After fixing the SSM path, you can try **`attach_fis_extension=True`** on the secondary in `app.py` if `aws ssm get-parameter --name /aws/service/fis/lambda-extension/AWS-FIS-extension-x86_64/1.x.x --region eu-central-1` succeeds. Failover and the FIS experiment work either way: chaos targets the **us-east-1** function; Route 53 fails over to **eu-central-1** when the primary is unhealthy.

To override the parameter name for all stacks that attach the extension, pass e.g.  
`-c ordersFisLayerSsmParameterName=/aws/service/fis/lambda-extension/AWS-FIS-extension-x86_64/1.0.0`  
(only if AWS documents a different suffix for your Region).

If you **add a new Lambda** by hand or temporarily remove the layer, attach the extension again before starting a Lambda-targeted FIS action:

1. In **Lambda → Layers → Add layer → AWS layers**, choose the FIS extension, **or** add an ARN resolved from SSM in that region (CLI):  
   `aws ssm get-parameter --name /aws/service/fis/lambda-extension/AWS-FIS-extension-x86_64/1.x.x --query Parameter.Value --output text --region us-east-1`
2. Set the two environment variables above; the **S3 bucket** in that region must allow the execution role to **ListBucket** (prefix `FisConfigs/`) and **GetObject** on `FisConfigs/*`, and the role needs **`fis:GetExperiment`** / **`fis:ListExperiments`** so the extension can resolve active experiments.

Without the extension and S3 location, **`aws:lambda:invocation-error`** will not affect real invocations the way this demo expects.

## Run the FIS experiment

1. In **AWS FIS** (console, **us-east-1**), open **Experiment templates** and find the template created by `OrdersRegionalUsEast1` (tag `Experiment=OrdersDemo-InvocationError`), or use the API with that template id.
2. Start an experiment from **us-east-1**. The template targets **`orders-api-us-east-1`** by **function ARN** (not by tag), so it does not pick up unrelated Lambdas such as CDK custom-resource providers.
3. The template runs **1 minute** with **`invocationPercentage=100`** and uses a **stop condition** tied to the CloudWatch alarm **`orders-lambda-errors-stop-us-east-1`** (Lambda **Errors** sum **> 10** in one minute) so the run aborts if errors spike beyond the safety threshold.

## What to observe

- **Route 53**: Health check on the **primary** (us-east-1) ALB `/health` should go **unhealthy** while the primary Lambda fails; the **failover** alias should shift answers toward the **secondary** (eu-central-1) ALB once health criteria are met.
- **CloudWatch**: In **us-east-1**, Lambda **Errors** and **Invocations** for `orders-api-us-east-1` (name from outputs) should reflect injected errors.
- **Traffic**: Clients using the **private** name `api.orders.internal` must resolve the hosted zone (associated to the **default VPC** in **us-east-1** on the global stack). From that VPC, `curl http://api.orders.internal/health` and `/orders` should eventually show **`eu-central-1`** in the health JSON during failover.

## Verify failover with curl

- **Direct ALB test (any network path that can reach the ALB):**  
  `curl -sS http://<AlbDnsName>/health` — compare `region` between primary and secondary DNS names from outputs.
- **Via Route 53 name:** From a host that uses the **private** `orders.internal` zone (same VPC association as the hosted zone), run:

```bash
curl -sS http://api.orders.internal/health
```

The JSON field **`region`** should read **`us-east-1`** when primary is healthy and **`eu-central-1`** after failover while the primary path is unhealthy.

If you are not in that VPC, use the **regional ALB DNS names** from stack outputs for the same check.

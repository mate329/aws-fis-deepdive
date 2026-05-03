#!/usr/bin/env python3
"""
test_lambda_apigw_alarm.py
--------------------------
Exercises the LambdaApigwAlarmFisStack: call the HTTP API, start FIS invocation-error,
hammer the API until Lambda Errors drive the CloudWatch alarm to ALARM, then stop FIS.

Prerequisites:
    cdk deploy LambdaApigwAlarmFisStack --context account=YOUR_ACCOUNT_ID

Usage:
    python test_lambda_apigw_alarm.py
    python test_lambda_apigw_alarm.py --stack-name LambdaApigwAlarmFisStack --region us-east-1
    python test_lambda_apigw_alarm.py --skip-fis   # only HTTP checks (no experiment)
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from typing import Any

import boto3

DEFAULT_STACK = "LambdaApigwAlarmFisStack"
DEFAULT_REGION = "us-east-1"
FIS_EXPERIMENT_TAG = "Lambda-ApigwAlarm-InvocationError"
WARMUP_SLEEP_S = 20
ALARM_POLL_S = 15
# Lambda Errors can take 1–2 minutes to appear in a 1-minute alarm period.
ALARM_TIMEOUT_S = 420


def stack_outputs(stack_name: str, region: str) -> dict[str, str]:
    cfn = boto3.client("cloudformation", region_name=region)
    resp = cfn.describe_stacks(StackName=stack_name)
    stacks = resp.get("Stacks") or []
    if not stacks:
        raise RuntimeError(f"Stack {stack_name!r} not found in {region}")
    out: dict[str, str] = {}
    for o in stacks[0].get("Outputs") or []:
        k = o.get("OutputKey")
        v = o.get("OutputValue")
        if k and v is not None:
            out[k] = v
    return out


def find_fis_template_id(region: str, experiment_tag: str) -> str:
    fis_client = boto3.client("fis", region_name=region)
    paginator = fis_client.get_paginator("list_experiment_templates")
    for page in paginator.paginate():
        for tmpl in page.get("experimentTemplates") or []:
            if tmpl.get("tags", {}).get("Experiment") == experiment_tag:
                return tmpl["id"]
    raise RuntimeError(
        f"No FIS template with tag Experiment={experiment_tag!r}. Deploy the stack first."
    )


def http_get(url: str, timeout: float = 30.0) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.getcode(), body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, body
    except urllib.error.URLError as e:
        return -1, str(e.reason)


def alarm_state(cw: Any, alarm_name: str) -> str:
    resp = cw.describe_alarms(AlarmNames=[alarm_name])
    alarms = resp.get("MetricAlarms") or []
    if not alarms:
        return "UNKNOWN"
    return alarms[0].get("StateValue", "UNKNOWN")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test APIGW + Lambda + CloudWatch alarm with FIS invocation-error",
    )
    parser.add_argument("--stack-name", default=DEFAULT_STACK)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument(
        "--skip-fis",
        action="store_true",
        help="Only hit the API; do not start/stop FIS (baseline 200s only).",
    )
    args = parser.parse_args()

    print("\n  Lambda APIGW + CloudWatch alarm FIS test")
    print(f"  Stack: {args.stack_name}  Region: {args.region}\n")

    try:
        outs = stack_outputs(args.stack_name, args.region)
    except Exception as e:
        print(f"  ❌ CloudFormation: {e}\n")
        sys.exit(1)

    api_url = outs.get("HttpApiUrl")
    alarm_name = outs.get("LambdaErrorsAlarmName")
    if not api_url or not alarm_name:
        print("  ❌ Stack outputs missing HttpApiUrl or LambdaErrorsAlarmName.\n")
        sys.exit(1)

    # Ensure trailing slash works for default route
    test_url = api_url if api_url.endswith("/") else f"{api_url}/"
    cw = boto3.client("cloudwatch", region_name=args.region)
    fis_client = boto3.client("fis", region_name=args.region)

    print("  1) Baseline GET (expect HTTP 200)")
    code, body = http_get(test_url)
    print(f"     HTTP {code}  body[:120]={body[:120]!r}")
    if code != 200:
        print("  ❌ Baseline failed; fix deployment or URL.\n")
        sys.exit(1)

    print(f"     Alarm state: {alarm_state(cw, alarm_name)}")

    if args.skip_fis:
        print("\n  --skip-fis: done.\n")
        return

    try:
        template_id = find_fis_template_id(args.region, FIS_EXPERIMENT_TAG)
    except RuntimeError as e:
        print(f"  ❌ {e}\n")
        sys.exit(1)

    print(f"\n  2) Starting FIS experiment  template_id={template_id}")
    start_resp = fis_client.start_experiment(experimentTemplateId=template_id)
    exp_id = start_resp["experiment"]["id"]
    print(f"     experiment_id={exp_id}")
    print(f"     Waiting {WARMUP_SLEEP_S}s for extension / fault config...\n")
    time.sleep(WARMUP_SLEEP_S)

    print("  3) Calling API until alarm goes ALARM (Lambda Errors ≥ 1 / minute)")
    attempt = 0
    deadline = time.time() + ALARM_TIMEOUT_S
    last_code = None
    while time.time() < deadline:
        attempt += 1
        last_code, _ = http_get(test_url, timeout=15.0)
        if attempt % 5 == 1 or last_code >= 400:
            print(f"     attempt {attempt}: HTTP {last_code}")
        state = alarm_state(cw, alarm_name)
        if state == "ALARM":
            print(f"\n  ✅ CloudWatch alarm {alarm_name!r} is ALARM after {attempt} request(s).")
            break
        time.sleep(2)
    else:
        print(f"\n  ❌ Alarm did not reach ALARM within {ALARM_TIMEOUT_S}s (last HTTP {last_code}).")
        print("     Stop the experiment in the console if needed.\n")
        try:
            fis_client.stop_experiment(id=exp_id)
        except Exception:
            pass
        sys.exit(1)

    print(f"\n  4) Stopping FIS experiment {exp_id}")
    try:
        fis_client.stop_experiment(id=exp_id)
    except Exception as e:
        print(f"     ⚠️ stop_experiment: {e}")

    print("     Waiting up to 90s for alarm to return to OK (optional)...")
    for _ in range(18):
        time.sleep(5)
        st = alarm_state(cw, alarm_name)
        print(f"     alarm state: {st}")
        if st == "OK":
            break

    print("\n  5) Post-stop GET (expect HTTP 200 again)")
    code, body = http_get(test_url)
    print(f"     HTTP {code}  body[:120]={body[:120]!r}\n")
    if code != 200:
        print("  ⚠️ Expected 200 after FIS stop; extension may still be draining.\n")

    print("  Done.\n")


if __name__ == "__main__":
    main()

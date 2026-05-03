#!/usr/bin/env python3
"""
test_multi_region_orders.py
-----------------------------
Integration checks for the multi_region_lambdas stacks (ALB → Lambda), optional FIS
invocation-error run against the primary region, and optional handler unit tests (no AWS).

Prerequisites (integration):
    cdk deploy OrdersGlobalStack OrdersRegionalUsEast1 OrdersRegionalEuCentral1 \\
        --context account=YOUR_ACCOUNT_ID
    (plus Route 53 context on global stack if you want --try-route53)

Usage:
    python test_multi_region_orders.py
    python test_multi_region_orders.py --with-fis
    python test_multi_region_orders.py --with-fis --recovery-timeout 300
    python test_multi_region_orders.py --try-route53
    python test_multi_region_orders.py --unit-only
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import unittest
from typing import Any
from unittest import mock

import boto3
import urllib.error
import urllib.request

DEFAULT_PRIMARY_STACK = "OrdersRegionalUsEast1"
DEFAULT_SECONDARY_STACK = "OrdersRegionalEuCentral1"
DEFAULT_GLOBAL_STACK = "OrdersGlobalStack"
PRIMARY_REGION = "us-east-1"
SECONDARY_REGION = "eu-central-1"
GLOBAL_REGION = "us-east-1"
FIS_EXPERIMENT_TAG = "OrdersDemo-InvocationError"
FIS_WARMUP_S = 25
FIS_HAMMER_TIMEOUT_S = 300
# After stop_experiment, FIS + ALB target health need time to clear 502s.
POST_STOP_EXPERIMENT_STATE_WAIT_S = 90
PRIMARY_RECOVERY_TIMEOUT_S = 180
PRIMARY_RECOVERY_POLL_S = 4


def _log_expected(msg: str) -> None:
    print(f"  [EXPECTED] {msg}")


def _log_observed(msg: str) -> None:
    print(f"  [OBSERVED]   {msg}")


def _fis_experiment_status(fis_client: Any, exp_id: str) -> str:
    resp = fis_client.get_experiment(id=exp_id)
    exp = resp.get("experiment") or resp
    state = exp.get("state")
    if isinstance(state, dict):
        return str(state.get("status") or state.get("Status") or "")
    if isinstance(state, str):
        return state
    return str(exp.get("status") or "")


def _wait_fis_experiment_not_running(
    fis_client: Any, exp_id: str, timeout_s: int
) -> str:
    """Block until status is no longer initiating/running/stopping, or timeout."""
    deadline = time.time() + timeout_s
    last = ""
    pending = {"initiating", "running", "stopping"}
    while time.time() < deadline:
        try:
            last = _fis_experiment_status(fis_client, exp_id)
        except Exception as e:
            last = f"(error: {e})"
        low = last.lower()
        if low and low not in pending:
            return last
        time.sleep(2)
    return last or "UNKNOWN"


def _wait_primary_alb_health_200(alb_dns: str, timeout_s: int, poll_s: float) -> tuple[bool, int, str]:
    """Poll GET /health until HTTP 200 or timeout. Returns (ok, last_code, last_body_prefix)."""
    deadline = time.time() + timeout_s
    last_code, last_body = -1, ""
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        last_code, last_body = http_get(f"http://{alb_dns.rstrip('/')}/health", timeout=20.0)
        if last_code == 200:
            return True, last_code, last_body[:200]
        _log_observed(f"recovery poll #{attempt}: HTTP {last_code} (body starts {last_body[:80]!r}…)")
        time.sleep(poll_s)
    return False, last_code, last_body[:200]


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


def find_fis_template_id(region: str, experiment_tag: str) -> str:
    fis_client = boto3.client("fis", region_name=region)
    paginator = fis_client.get_paginator("list_experiment_templates")
    for page in paginator.paginate():
        for tmpl in page.get("experimentTemplates") or []:
            if tmpl.get("tags", {}).get("Experiment") == experiment_tag:
                return tmpl["id"]
    raise RuntimeError(
        f"No FIS template with tag Experiment={experiment_tag!r} in {region}. "
        "Deploy OrdersRegionalUsEast1 first."
    )


def check_alb_health_and_orders(label: str, alb_dns: str, expect_region: str) -> None:
    base = f"http://{alb_dns.rstrip('/')}"
    h_url = f"{base}/health"
    o_url = f"{base}/orders"

    code, body = http_get(h_url)
    _log_observed(f"{label} GET /health → HTTP {code}")
    if code != 200:
        raise RuntimeError(f"{label} /health expected 200, got {code}: {body[:200]!r}")
    data = json.loads(body)
    if data.get("status") != "ok":
        raise RuntimeError(f"{label} /health JSON missing status=ok: {data!r}")
    got = data.get("region", "")
    if got != expect_region:
        raise RuntimeError(
            f"{label} /health region expected {expect_region!r}, got {got!r}. "
            "Check you mapped the correct stack outputs to primary vs secondary."
        )

    code, body = http_get(o_url)
    _log_observed(f"{label} GET /orders → HTTP {code}")
    if code != 200:
        raise RuntimeError(f"{label} /orders expected 200, got {code}: {body[:300]!r}")
    data = json.loads(body)
    orders = data.get("orders")
    if not isinstance(orders, list):
        raise RuntimeError(f"{label} /orders expected {{'orders': [...]}}, got keys {data.keys()}")
    if len(orders) < 3:
        raise RuntimeError(f"{label} /orders expected at least 3 items, got {len(orders)}")


def run_integration(args: argparse.Namespace) -> int:
    print("\n  Multi-region orders ALB + Lambda integration")
    print(f"  Primary stack:   {args.primary_stack}  ({args.primary_region})")
    print(f"  Secondary stack: {args.secondary_stack}  ({args.secondary_region})\n")

    try:
        p_out = stack_outputs(args.primary_stack, args.primary_region)
        s_out = stack_outputs(args.secondary_stack, args.secondary_region)
    except Exception as e:
        print(f"  ❌ CloudFormation: {e}\n")
        return 1

    p_dns = p_out.get("AlbDnsName")
    s_dns = s_out.get("AlbDnsName")
    if not p_dns or not s_dns:
        print("  ❌ Missing AlbDnsName on one or both regional stacks.\n")
        return 1

    try:
        _log_expected(
            "Baseline: primary ALB responds HTTP 200 on /health and /orders; "
            "/health JSON includes region=us-east-1."
        )
        check_alb_health_and_orders("primary", p_dns, "us-east-1")
        _log_expected(
            "Baseline: secondary ALB responds HTTP 200; /health JSON includes region=eu-central-1."
        )
        check_alb_health_and_orders("secondary", s_dns, "eu-central-1")
        print("  ✅ Baseline smoke checks passed.\n")
    except RuntimeError as e:
        print(f"  ❌ {e}\n")
        return 1

    if args.try_route53:
        try:
            g_out = stack_outputs(args.global_stack, args.global_region)
        except Exception as e:
            print(f"  ⚠️ Global stack outputs: {e} (skip Route 53 HTTP)\n")
            g_out = {}
        fqdn = g_out.get("Route53ApiFqdn")
        if not fqdn:
            print(
                "  ⚠️ No Route53ApiFqdn output (redeploy OrdersGlobalStack with ALB context). "
                "Skipping Route 53 HTTP probe.\n"
            )
        else:
            url = f"http://{fqdn}/health"
            print(f"  Route 53 name probe: GET {url}")
            code, body = http_get(url)
            print(f"       HTTP {code}  body[:200]={body[:200]!r}")
            if code != 200:
                print(
                    "  ⚠️ Non-200 is common from a laptop (private zone only in us-east-1 default VPC).\n"
                )
            else:
                print("       OK\n")

    if not args.with_fis:
        print("  (Pass --with-fis to run a 1-minute template manually stopped after chaos is visible.)\n")
        return 0

    fis_client = boto3.client("fis", region_name=args.primary_region)
    try:
        template_id = find_fis_template_id(args.primary_region, FIS_EXPERIMENT_TAG)
    except RuntimeError as e:
        print(f"  ❌ {e}\n")
        return 1

    print("\n  --- FIS: aws:lambda:invocation-error (template duration PT1M after stack redeploy) ---\n")
    _log_expected(
        "After start_experiment, the primary orders Lambda may return errors; the ALB often "
        "surfaces that as HTTP 502 on /health until the experiment ends and the extension clears."
    )
    _log_expected(
        "The secondary region is NOT in the FIS target; its /health should stay HTTP 200 with "
        "region=eu-central-1 throughout."
    )

    _log_observed(f"Starting experiment from template_id={template_id}")
    start_resp = fis_client.start_experiment(experimentTemplateId=template_id)
    exp_id = start_resp["experiment"]["id"]
    _log_observed(f"experiment_id={exp_id}")
    _log_expected(
        f"Waiting {FIS_WARMUP_S}s so FIS can publish fault config and the Lambda extension can pick it up."
    )
    time.sleep(FIS_WARMUP_S)

    deadline = time.time() + FIS_HAMMER_TIMEOUT_S
    primary_bad = False
    attempt = 0
    _log_expected(
        "Polling primary ALB GET /health until we see a non-200 (confirms traffic hits an unhealthy target) "
        f"or {FIS_HAMMER_TIMEOUT_S}s elapses (unexpected if FIS is wired correctly)."
    )
    while time.time() < deadline:
        attempt += 1
        code, body = http_get(f"http://{p_dns}/health", timeout=15.0)
        if code != 200:
            primary_bad = True
            _log_observed(
                f"Primary poll #{attempt}: HTTP {code} — matches EXPECTED chaos / ALB error surface."
            )
            break
        if attempt == 1 or attempt % 10 == 0:
            _log_observed(f"Primary poll #{attempt}: HTTP {code} (still healthy; FIS may not be active yet)")
        time.sleep(2)

    if not primary_bad:
        print(
            f"  ⚠️  [UNEXPECTED] Primary stayed HTTP 200 for {FIS_HAMMER_TIMEOUT_S}s — "
            "check experiment ran, extension env vars, and S3 fault-config bucket.\n"
        )

    code, body = http_get(f"http://{s_dns}/health", timeout=15.0)
    _log_expected("Secondary /health stays HTTP 200 during primary chaos.")
    _log_observed(f"Secondary GET /health → HTTP {code}")
    if code == 200:
        data = json.loads(body)
        _log_observed(f"Secondary /health region={data.get('region')!r} (EXPECTED: 'eu-central-1')")
    else:
        _log_observed(f"Secondary body (truncated): {body[:200]!r}")
    print()

    _log_observed(f"Calling stop_experiment(id={exp_id})")
    try:
        fis_client.stop_experiment(id=exp_id)
    except Exception as e:
        print(f"  ❌ stop_experiment failed: {e}\n")
        return 1

    _log_expected(
        "After stop, FIS should leave initiating/running/stopping; then the Lambda + ALB need a short "
        f"grace period before /health is 200 again (often {PRIMARY_RECOVERY_POLL_S}–60s, sometimes longer)."
    )
    final_state = _wait_fis_experiment_not_running(
        fis_client, exp_id, timeout_s=POST_STOP_EXPERIMENT_STATE_WAIT_S
    )
    _log_observed(f"Last describe_experiment status (non-running wait): {final_state!r}")

    _log_expected(
        f"Polling primary GET /health until HTTP 200 or {args.recovery_timeout}s "
        "(EXPECTED: recovery after experiment ends; 502s right after stop are normal, not final)."
    )
    ok, last_code, last_snip = _wait_primary_alb_health_200(
        p_dns, timeout_s=args.recovery_timeout, poll_s=PRIMARY_RECOVERY_POLL_S
    )
    if ok:
        _log_observed(f"Primary /health recovered: HTTP 200 (body starts {last_snip!r}…)")
        print("  ✅ [EXPECTED MET] Primary ALB healthy again after FIS stop.\n")
    else:
        print(
            f"  ❌ [EXPECTED NOT MET] Primary /health still not HTTP 200 after {args.recovery_timeout}s "
            f"(last HTTP {last_code}). Check FIS console, Lambda errors, and ALB target health.\n"
        )
        return 1

    try:
        _log_expected("Sanity: primary /orders still works after recovery.")
        check_alb_health_and_orders("primary", p_dns, "us-east-1")
    except RuntimeError as e:
        print(f"  ❌ Post-recovery check failed: {e}\n")
        return 1

    print("  Done.\n")
    return 0


# --- Handler unit tests (no AWS) -------------------------------------------------


class TestOrdersHandler(unittest.TestCase):
    def setUp(self) -> None:
        import multi_region_lambdas.orders_lambda.handler as hmod

        hmod._table = None

    def test_health(self) -> None:
        from multi_region_lambdas.orders_lambda.handler import handler as orders_handler

        with mock.patch.dict(
            "os.environ",
            {"AWS_REGION": "us-east-1", "TABLE_NAME": "orders"},
            clear=False,
        ):
            out = orders_handler({"path": "/health", "httpMethod": "GET"}, None)
        self.assertEqual(out["statusCode"], 200)
        body = json.loads(out["body"])
        self.assertEqual(body.get("status"), "ok")
        self.assertEqual(body.get("region"), "us-east-1")

    def test_orders_scan(self) -> None:
        import multi_region_lambdas.orders_lambda.handler as hmod
        from multi_region_lambdas.orders_lambda.handler import handler as orders_handler

        fake_table = mock.MagicMock()
        fake_table.scan.return_value = {
            "Items": [{"orderId": "demo-1", "customer": "Ada"}],
        }
        fake_resource = mock.MagicMock()
        fake_resource.Table.return_value = fake_table

        with mock.patch.dict(
            "os.environ",
            {"AWS_REGION": "eu-central-1", "TABLE_NAME": "orders"},
            clear=False,
        ):
            with mock.patch.object(hmod, "boto3") as mock_boto:
                mock_boto.resource.return_value = fake_resource
                hmod._table = None
                out = orders_handler({"path": "/orders", "httpMethod": "GET"}, None)

        self.assertEqual(out["statusCode"], 200)
        body = json.loads(out["body"])
        self.assertIn("orders", body)
        self.assertEqual(len(body["orders"]), 1)

    def test_not_found(self) -> None:
        from multi_region_lambdas.orders_lambda.handler import handler as orders_handler

        with mock.patch.dict("os.environ", {"AWS_REGION": "us-east-1"}, clear=False):
            out = orders_handler({"path": "/nope", "httpMethod": "GET"}, None)
        self.assertEqual(out["statusCode"], 404)


def run_unit_tests() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestOrdersHandler)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test multi_region_lambdas orders demo (ALB + Lambda + optional FIS).",
    )
    parser.add_argument("--primary-stack", default=DEFAULT_PRIMARY_STACK)
    parser.add_argument("--secondary-stack", default=DEFAULT_SECONDARY_STACK)
    parser.add_argument("--primary-region", default=PRIMARY_REGION)
    parser.add_argument("--secondary-region", default=SECONDARY_REGION)
    parser.add_argument("--global-stack", default=DEFAULT_GLOBAL_STACK)
    parser.add_argument("--global-region", default=GLOBAL_REGION)
    parser.add_argument(
        "--with-fis",
        action="store_true",
        help=(
            "Start FIS invocation-error in us-east-1, confirm primary degrades and secondary stays OK, "
            "stop the experiment, then wait until primary /health is 200 again (or fail the run)."
        ),
    )
    parser.add_argument(
        "--recovery-timeout",
        type=int,
        default=PRIMARY_RECOVERY_TIMEOUT_S,
        metavar="SECONDS",
        help=(
            "Max seconds to poll primary /health for HTTP 200 after FIS stop (default: "
            f"{PRIMARY_RECOVERY_TIMEOUT_S}). Increase if ALB target health is slow to flip healthy."
        ),
    )
    parser.add_argument(
        "--try-route53",
        action="store_true",
        help="If OrdersGlobalStack exports Route53ApiFqdn, GET http://<fqdn>/health (may fail off-VPC).",
    )
    parser.add_argument(
        "--unit-only",
        action="store_true",
        help="Only run handler unit tests (no boto3 / stacks).",
    )
    args = parser.parse_args()

    if args.unit_only:
        return run_unit_tests()

    return run_integration(args)


if __name__ == "__main__":
    sys.exit(main())

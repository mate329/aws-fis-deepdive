#!/usr/bin/env python3
"""
test_global_chaos.py
--------------------
Test script for the Global Table FIS chaos experiment.
Shows stale reads, routing fallback, and eventual consistency
in real time across us-east-1 (primary) and eu-central-1 (replica).

Usage:
    # Baseline — everything healthy
    python test_global_chaos.py --scenario baseline

    # Version drift during active replication-pause experiment:
    python test_global_chaos.py --scenario drift

    # Lambda invocation-delay experiment (self-managed):
    python test_global_chaos.py --scenario delay

    # Lambda invocation-error experiment (self-managed):
    python test_global_chaos.py --scenario error

    # Start FIS GlobalTable-PauseAndReroute in the AWS console, then run:
    python test_global_chaos.py --watch

    # Full scenario suite (excludes --watch, --scenario delay, and --scenario error)
    python test_global_chaos.py --scenario all
"""

import argparse
import json
import sys
import time
import boto3
from datetime import datetime

# ── Config ───────────────────────────────────────────────────────────────────
PRIMARY_REGION  = "us-east-1"
REPLICA_REGION  = "eu-central-1"
WRITER_FUNCTION = "fis-global-writer"
READER_FUNCTION = "fis-global-reader"
WATCH_INTERVAL  = 3   # seconds between write→read cycles in watch mode

writer = boto3.client("lambda", region_name=PRIMARY_REGION)
reader = boto3.client("lambda", region_name=PRIMARY_REGION)   # reader Lambda deployed to primary for demo
fis_client = boto3.client("fis",    region_name=PRIMARY_REGION)


# ── FIS helpers ──────────────────────────────────────────────────────────────

def find_experiment_template_id(experiment_tag: str) -> str:
    """
    Returns the FIS experiment template ID whose 'Experiment' tag matches
    experiment_tag.  Raises RuntimeError if not found.
    """
    paginator = fis_client.get_paginator("list_experiment_templates")
    for page in paginator.paginate():
        for tmpl in page["experimentTemplates"]:
            if tmpl.get("tags", {}).get("Experiment") == experiment_tag:
                return tmpl["id"]
    raise RuntimeError(f"FIS template with tag Experiment={experiment_tag!r} not found. Run `cdk deploy`.")


def start_fis_experiment(template_id: str) -> str:
    resp = fis_client.start_experiment(experimentTemplateId=template_id)
    return resp["experiment"]["id"]


def stop_fis_experiment(exp_id: str):
    try:
        fis_client.stop_experiment(id=exp_id)
    except Exception as e:
        print(f"  ⚠️  Could not stop experiment {exp_id}: {e}")


# ── Invoke helpers ————————————————————————————————————————————───────────
def invoke_writer(pk: str, sk: str = "default", payload: dict = None) -> dict:
    event = {"pk": pk, "sk": sk, "payload": payload or {}}
    start = time.time()
    resp = writer.invoke(
        FunctionName=WRITER_FUNCTION,
        InvocationType="RequestResponse",
        Payload=json.dumps(event),
    )
    ms = int((time.time() - start) * 1000)
    body = json.loads(resp["Payload"].read())
    return {"status": body.get("statusCode"), "ms": ms, "data": json.loads(body.get("body", "{}"))}


def invoke_reader(pk: str, sk: str = "default") -> dict:
    event = {"pk": pk, "sk": sk}
    start = time.time()
    resp = reader.invoke(
        FunctionName=READER_FUNCTION,
        InvocationType="RequestResponse",
        Payload=json.dumps(event),
    )
    ms = int((time.time() - start) * 1000)
    body = json.loads(resp["Payload"].read())
    return {"status": body.get("statusCode"), "ms": ms, "data": json.loads(body.get("body", "{}"))}


# ── Display helpers ───────────────────────────────────────────────────────────
def fmt_write(result: dict, pk: str) -> str:
    status = result["status"]
    icon = "✅" if status == 200 else ("⚠️ " if status == 409 else "❌")
    ver = result["data"].get("version", "?")
    return f"  {icon} WRITE  pk={pk:<22} version={ver}  {result['ms']}ms"


def fmt_read(result: dict, pk: str) -> str:
    status = result["status"]
    data   = result["data"]

    if status == 404:
        return f"  🔍 READ   pk={pk:<22} NOT FOUND (replica likely stale)  {result['ms']}ms"

    if status != 200:
        return f"  ❌ READ   pk={pk:<22} ERROR {status}  {result['ms']}ms"

    item      = data.get("item", {})
    read_from = data.get("read_from", "?")
    reason    = data.get("routing_reason", "?")
    replica_ver   = item.get("version", "?")
    written_at_ms = item.get("written_at", 0)
    age_ms        = int(time.time() * 1000) - int(written_at_ms) if written_at_ms else 0

    region_badge = f"[{read_from.upper():<7}]"
    stale_badge  = " ⚠️  STALE FALLBACK"       if reason == "stale_data" else ""
    miss_badge   = " 🔍 REPLICA-MISS-FALLBACK" if reason == "replica_miss_fallback" else ""
    ssm_badge    = " 🔀 SSM-REROUTED"           if reason == "ssm_flag" else ""
    err_badge    = " 🚨 REPLICA-ERROR-FALLBACK" if reason == "replica_error_fallback" else ""

    return (
        f"  📖 READ   pk={pk:<22} {region_badge} "
        f"version={replica_ver}  age={age_ms}ms  {result['ms']}ms"
        f"{stale_badge}{miss_badge}{ssm_badge}{err_badge}"
    )


def section(title: str):
    print(f"\n{'='*62}")
    print(f"  {title}")
    print(f"{'='*62}\n")


def subsection(title: str):
    print(f"\n  ── {title} {'─'*(55-len(title))}")


# ── Scenarios ─────────────────────────────────────────────────────────────────
def scenario_baseline():
    """
    Healthy state: write to primary, read back from replica.
    Demonstrates normal replication working correctly.
    """
    section("SCENARIO 1: Baseline — Healthy Replication")
    print("  Write 5 items to primary (us-east-1), then read from replica (eu-central-1).")
    print("  All reads should return the latest version with no staleness.\n")

    pks = []
    subsection("Writes → us-east-1")
    for i in range(5):
        pk = f"user#{int(time.time())}-{i}"
        result = invoke_writer(pk, payload={"name": f"User {i}", "score": i * 10})
        print(fmt_write(result, pk))
        pks.append(pk)

    print("\n  Waiting 2s for replication...\n")
    time.sleep(2)

    subsection("Reads ← eu-central-1 replica")
    for pk in pks:
        result = invoke_reader(pk)
        print(fmt_read(result, pk))

    print()


def scenario_stale_reads():
    """
    Simulates what stale reads look like WITHOUT the FIS experiment.
    We write then immediately read — replica may not have caught up yet.
    """
    section("SCENARIO 2: Immediate Read-After-Write (No Sleep)")
    print("  Write then instantly read — races against replication.")
    print("  You may see stale fallback to primary for very recent writes.\n")

    subsection("Write then instantly read (×10)")
    stale_count = 0
    primary_count = 0

    for i in range(10):
        pk = f"race#{int(time.time() * 1000)}-{i}"
        write_result = invoke_writer(pk, payload={"iteration": i})
        read_result  = invoke_reader(pk)

        write_line = fmt_write(write_result, pk)
        read_line  = fmt_read(read_result, pk)
        print(write_line)
        print(read_line)

        read_from = read_result["data"].get("read_from", "")
        stale     = read_result["data"].get("stale_detected", False)
        if stale:
            stale_count += 1
        if read_from == "primary":
            primary_count += 1
        print()

    print(f"  Summary: {stale_count}/10 stale fallbacks detected")
    print(f"           {primary_count}/10 reads ultimately served from primary\n")


def scenario_version_drift():
    """
    Write the same item repeatedly and read after each write.
    Shows version numbers drifting between replica and primary
    when replication is paused.
    """
    section("SCENARIO 3: Version Drift — Repeated Writes to Same Item")
    print("  Write to the same pk 8 times, reading after each write.")
    print("  Run this DURING the FIS pause-replication experiment.")
    print("  Replica version will lag behind primary version.\n")

    pk = f"drift#{int(time.time())}"
    sk = "counter"

    for i in range(8):
        write_result = invoke_writer(pk, sk=sk, payload={"counter": i})
        write_ver = write_result["data"].get("version", "?")
        print(fmt_write(write_result, pk))

        read_result = invoke_reader(pk)
        read_ver  = read_result["data"].get("item", {}).get("version", "?") if read_result["status"] == 200 else "?"
        read_from = read_result["data"].get("read_from", "?")
        print(fmt_read(read_result, pk))

        # Highlight drift
        if str(write_ver) != str(read_ver):
            drift = int(write_ver) - int(read_ver) if str(write_ver).isdigit() and str(read_ver).isdigit() else "?"
            print(f"  {'':>6}↑ VERSION DRIFT: primary=v{write_ver}, {read_from}=v{read_ver}  (lag: {drift} version(s))")

        print()
        time.sleep(1)


def scenario_lambda_delay():
    """
    Uses aws:lambda:invocation-add-delay to inject a 12-second pre-handler
    startup delay into every reader Lambda invocation for 2 minutes.

    Because STALENESS_THRESHOLD_MS = 10 000 ms, items written just before the
    reader is called appear stale by the time the handler actually executes —
    even though DynamoDB replication is completely healthy.  This demonstrates
    a different failure mode: latency-induced apparent staleness.

    The scenario starts and stops the FIS experiment automatically.
    """
    section("SCENARIO 4: Lambda Invocation Delay  (aws:lambda:invocation-add-delay)")
    print("  Starts the Lambda-InvocationDelay FIS experiment automatically.")
    print("  Writes 5 items, then reads each one back under a 12 s injected delay.")
    print("  Delay > STALENESS_THRESHOLD_MS (10 s) → stale-data fallback fires")
    print("  even though replication itself is completely healthy.\n")

    try:
        template_id = find_experiment_template_id("Lambda-InvocationDelay")
    except RuntimeError as e:
        print(f"  ❌ {e}\n")
        return

    print(f"  ▶ Starting FIS experiment  template={template_id}")
    exp_id = start_fis_experiment(template_id)
    print(f"    Experiment {exp_id} running. Waiting 20 s for extension to activate...\n")
    time.sleep(20)

    pks = []
    subsection("Writes → primary  (writer has no delay injected)")
    for i in range(5):
        pk = f"delay#{int(time.time() * 1000)}-{i}"
        result = invoke_writer(pk, payload={"iteration": i})
        print(fmt_write(result, pk))
        pks.append(pk)

    print()
    subsection("Reads ← reader  (+12 s FIS startup delay per call)")
    print("  Each read will take ~12 s. Age = (12 s delay) + (write→read gap)\n")
    for pk in pks:
        result = invoke_reader(pk)
        print(fmt_read(result, pk))

    print()
    print(f"  ■ Stopping FIS experiment {exp_id}...")
    stop_fis_experiment(exp_id)
    print("    Experiment stopped. Reader latency has returned to normal.\n")

    print("  Key takeaway: latency-induced apparent staleness is a separate failure")
    print("  mode from replication lag. The data is consistent — the reader is just")
    print("  too slow to know that.\n")


def scenario_lambda_error():
    """
    Uses aws:lambda:invocation-error with preventExecution=true to make the
    reader Lambda return errors on 100% of invocations without running the
    handler.  Exercises the replica_error_fallback path: the client sees a
    non-200/404, and the reader's ClientError handler falls back to a direct
    primary read.
    """
    section("SCENARIO 5: Lambda Invocation Error  (aws:lambda:invocation-error)")
    print("  Starts the Lambda-InvocationError FIS experiment automatically.")
    print("  Every reader call returns an error (handler never runs).")
    print("  Shows what happens when the reader function itself is unavailable.\n")

    try:
        template_id = find_experiment_template_id("Lambda-InvocationError")
    except RuntimeError as e:
        print(f"  ❌ {e}\n")
        return

    print(f"  ▶ Starting FIS experiment  template={template_id}")
    exp_id = start_fis_experiment(template_id)
    print(f"    Experiment {exp_id} running. Waiting 20 s for extension to activate...\n")
    time.sleep(20)

    subsection("Writes → primary  (writer is unaffected)")
    pks = []
    for i in range(5):
        pk = f"err#{int(time.time() * 1000)}-{i}"
        result = invoke_writer(pk, payload={"iteration": i})
        print(fmt_write(result, pk))
        pks.append(pk)

    print()
    subsection("Reads ← reader  (FIS forces error on every invocation)")
    print("  The reader's ClientError handler catches the injected error")
    print("  and falls back to a direct primary read (replica_error_fallback).\n")
    for pk in pks:
        result = invoke_reader(pk)
        print(fmt_read(result, pk))
        # With preventExecution=true the Lambda itself never runs, so the
        # response comes back as a Lambda-level error, not a 500 from the handler.
        if result["status"] is None or result["status"] not in (200, 404):
            print(f"    raw status={result['status']}  (FIS error injection active)")

    print()
    print(f"  ■ Stopping FIS experiment {exp_id}...")
    stop_fis_experiment(exp_id)
    print("    Experiment stopped.\n")

    print("  Key takeaway: reader Lambda errors trigger last-resort primary fallback.")
    print("  The system degrades gracefully even when the reader is completely broken.\n")


# ── Watch mode ────────────────────────────────────────────────────────────────
def watch_mode():
    """
    Continuous write→read loop with live health display.
    Run this DURING an FIS experiment to observe the transition
    from normal replication → stale reads → routing fallback → recovery.
    """
    section("WATCH MODE — Live Replication Health")
    print("  Start your FIS experiment (GlobalTable-PauseAndReroute, 3 min) in the AWS console NOW.")
    print("  This loop writes a value then immediately reads it back.")
    print("  Watch for ⚠️  STALE FALLBACK (one batch), then 🔀 SSM-REROUTED for ~2.5 min,")
    print("  and finally 📖 READ [REPLICA] once replication recovers.")
    print("\n  Press Ctrl+C to stop.\n")

    pk = f"watch#{int(time.time())}"   # fixed pk so version drift is visible
    batch = 0
    stats = {"total": 0, "stale": 0, "rerouted": 0, "errors": 0}

    try:
        while True:
            batch += 1
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"  ── Batch #{batch:03d}  {ts}  pk={pk} {'─'*20}")

            # Write new value
            write_result = invoke_writer(pk, payload={"batch": batch, "ts": ts})
            print(fmt_write(write_result, pk))

            # Read it back
            read_result = invoke_reader(pk)
            print(fmt_read(read_result, pk))

            # Update live stats
            stats["total"] += 1
            if read_result["data"].get("stale_detected"):
                stats["stale"] += 1
            if read_result["data"].get("routing_reason") == "ssm_flag":
                stats["rerouted"] += 1
            if read_result["status"] not in (200, 404):
                stats["errors"] += 1

            # Health bar
            stale_pct = int(stats["stale"] / stats["total"] * 100)
            bar_len   = 20
            filled    = int(stale_pct / 100 * bar_len)
            bar       = "█" * filled + "░" * (bar_len - filled)
            health_icon = "🔴" if stale_pct > 30 else ("🟡" if stale_pct > 0 else "🟢")
            print(
                f"\n  {health_icon} Staleness rate: [{bar}] {stale_pct}%  "
                f"(stale={stats['stale']} rerouted={stats['rerouted']} errors={stats['errors']} / {stats['total']} total)"
            )
            print(f"\n  Next in {WATCH_INTERVAL}s...\n")
            time.sleep(WATCH_INTERVAL)

    except KeyboardInterrupt:
        print("\n\n  Watch mode stopped.")
        print(f"  Final stats: {json.dumps(stats, indent=2)}\n")


# ── Main ──────────────────────────────────────────────────────────────────────
def check_connectivity():
    for fn, client, region in [
        (WRITER_FUNCTION, writer, PRIMARY_REGION),
        (READER_FUNCTION, reader, PRIMARY_REGION),
    ]:
        try:
            client.get_function(FunctionName=fn)
        except Exception as e:
            print(f"\n  ❌ Cannot reach '{fn}' in {region}: {e}")
            print(f"     Did you run `cdk deploy`?\n")
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Global Table FIS Chaos Test")
    parser.add_argument(
        "--scenario",
        choices=["baseline", "stale", "drift", "delay", "error", "all"],
        default="all",
        help="Which scenario to run (default: all; 'delay'/'error' manage their own FIS experiment)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Watch mode: continuous write/read loop, run during FIS experiment",
    )
    args = parser.parse_args()

    print("\n  🌍 Global Table FIS Chaos Test Runner")
    print(f"  Primary : {PRIMARY_REGION}  (writer)")
    print(f"  Replica : {REPLICA_REGION}  (reader default)")
    print(f"  Writer  : {WRITER_FUNCTION}")
    print(f"  Reader  : {READER_FUNCTION}")

    check_connectivity()

    if args.watch:
        watch_mode()
        return

    if args.scenario == "all":
        scenario_baseline()
        scenario_stale_reads()
        scenario_version_drift()
    elif args.scenario == "baseline":
        scenario_baseline()
    elif args.scenario == "stale":
        scenario_stale_reads()
    elif args.scenario == "drift":
        scenario_version_drift()
    elif args.scenario == "delay":
        scenario_lambda_delay()
    elif args.scenario == "error":
        scenario_lambda_error()

    print("  Done. 🎉\n")


if __name__ == "__main__":
    main()

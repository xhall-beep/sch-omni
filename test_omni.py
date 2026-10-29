#!/usr/bin/env python3
"""Hermetic tests for the SCH Omni orchestrator layer (omni.py).

Everything runs in temp dirs with fake executors — no live estate
sources, no network, no token file. Covers:

- pipelines: output of step 1 flows into step 2's params and changes
  its result (data-passing end-to-end); a failing step stops the run.
- sealed log: a written chain verifies clean; a tampered entry is
  detected at the right sequence number.
- scheduler: a due one-shot fires exactly once and never refires; a due
  interval fires once, advances on cadence, and does not burst; the
  store survives a reload from disk (restart persistence).
- CLI: `server.py --run-due` against a temp state dir fires a due
  one-shot (claims_today, read-only/local) and marks it fired.

Prints one PASS/FAIL line per check; exits non-zero on any failure.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import omni  # noqa: E402

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition)))
    line = "%s %s" % ("PASS" if condition else "FAIL", name)
    if detail:
        line += " — " + detail
    print(line, flush=True)


# ------------------------------------------------------------ pipelines

def test_pipeline_data_passing():
    calls = []

    def produce(params):
        calls.append(("produce", params))
        return {"value": 41, "label": "seed"}

    def increment(params):
        calls.append(("increment", params))
        return {"value": params["base"] + 1, "echo": params["note"]}

    fake = {"produce": produce, "increment": increment}
    omni.PIPELINES["test_chain"] = {
        "description": "hermetic test pipeline",
        "steps": [
            {"action": "produce", "save_as": "first", "params": {}},
            {"action": "increment", "save_as": "second",
             "params": {"base": "{steps.first.value}",
                        "note": "label was {steps.first.label}"}},
        ],
    }
    try:
        record = omni.run_pipeline(
            "test_chain", {}, lambda a, p: fake[a](p))
    finally:
        del omni.PIPELINES["test_chain"]
    second = record["steps"][1]
    check("pipeline runs end-to-end",
          record["ok"] is True and len(record["steps"]) == 2)
    check("pipeline passes step-1 output into step-2 params",
          second["params"]["base"] == 41
          and second["params"]["note"] == "label was seed",
          "resolved params=%s" % (second["params"],))
    check("pipeline step-2 result depends on step-1 output",
          second["result"]["value"] == 42,
          "value=%s" % second["result"].get("value"))
    check("pipeline record carries the step context",
          record["context"]["steps"]["first"]["value"] == 41
          and record["context"]["steps"]["second"]["value"] == 42)


def test_pipeline_failure_stops():
    ran = []

    def boom(params):
        raise RuntimeError("source down")

    def never(params):
        ran.append(True)
        return {}

    fake = {"boom": boom, "never": never}
    omni.PIPELINES["test_fail"] = {
        "description": "hermetic failure pipeline",
        "steps": [{"action": "boom", "params": {}},
                  {"action": "never", "params": {}}],
    }
    try:
        record = omni.run_pipeline(
            "test_fail", {}, lambda a, p: fake[a](p))
    finally:
        del omni.PIPELINES["test_fail"]
    check("pipeline stops at the failing step",
          record["ok"] is False and len(record["steps"]) == 1
          and record["steps"][0]["error"] == "source down"
          and not ran)


# ------------------------------------------------------------ sealed log

def test_sealed_log(tmp):
    path = os.path.join(tmp, "log.jsonl")
    log = omni.SealedLog(path)
    log.append("action", "claims_today", True)
    log.append("action", "sku_count", True)
    log.append("pipeline", "estate_snapshot", False, {"steps": 2})
    report = log.verify()
    check("sealed log chain verifies",
          report["ok"] is True and report["entries"] == 3,
          "report=%s" % (report,))

    # Tamper: rewrite the middle entry's name in place.
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    entry = json.loads(lines[1])
    entry["name"] = "forged_action"
    lines[1] = json.dumps(entry, sort_keys=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    fresh = omni.SealedLog(path)
    report = fresh.verify()
    check("sealed log detects a tampered entry",
          report["ok"] is False and report["first_bad_seq"] == 1,
          "report=%s" % (report,))


# ------------------------------------------------------------ scheduler

def test_scheduler(tmp):
    path = os.path.join(tmp, "schedules.json")
    store = omni.ScheduleStore(path)
    fired = []

    def dispatch(target_type, target, params):
        fired.append((target_type, target))
        return {"did": target}

    now = time.time()
    store.add("once", "action", "claims_today", {}, run_at=now - 5)
    interval = store.add("interval", "action", "sku_count", {},
                         every_seconds=3600, now=now - 7200)
    # The interval's first next_run is an hour in the past: overdue.
    store2 = omni.ScheduleStore(path)  # reload from disk = restart
    overdue = [s for s in store2.due(now) if s["id"] == interval["id"]]
    check("scheduler store survives reload from disk",
          len(store2.list()) == 2 and len(overdue) == 1)

    records = omni.run_due(store, dispatch, now=now)
    check("scheduler fires due entries once each",
          len(records) == 2 and all(r["ok"] for r in records)
          and len(fired) == 2,
          "records=%d fired=%s" % (len(records), fired))

    records = omni.run_due(store, dispatch, now=now)
    check("one-shot does not refire and interval does not burst",
          records == [] and len(fired) == 2)

    entry = store.get(interval["id"])
    check("interval advances on cadence after firing",
          entry["run_count"] == 1 and entry["next_run"] > now,
          "next_run-now=%.0fs" % (entry["next_run"] - now))
    once_entries = [s for s in store.list() if s["kind"] == "once"]
    check("one-shot disables itself after firing",
          once_entries and once_entries[0]["enabled"] is False
          and once_entries[0]["run_count"] == 1)


# ------------------------------------------------------------ CLI --run-due

def test_run_due_cli(tmp):
    state = os.path.join(tmp, "state")
    os.makedirs(state)
    store = omni.ScheduleStore(os.path.join(state, "schedules.json"))
    store.add("once", "action", "claims_today", {},
              run_at=time.time() - 5)
    env = dict(os.environ, SCH_OMNI_STATE_DIR=state)
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "server.py"), "--run-due"],
        capture_output=True, text=True, env=env, timeout=60)
    out = {}
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        pass
    check("server --run-due fires the due one-shot",
          proc.returncode == 0 and out.get("count") == 1
          and out["ran"][0]["ok"] is True
          and out["ran"][0]["target"] == "claims_today",
          "rc=%d out=%s" % (proc.returncode, str(out)[:160]))
    store = omni.ScheduleStore(os.path.join(state, "schedules.json"))
    entries = store.list()
    check("server --run-due marks the one-shot fired on disk",
          entries and entries[0]["enabled"] is False
          and entries[0]["run_count"] == 1)


def main():
    test_pipeline_data_passing()
    test_pipeline_failure_stops()
    with tempfile.TemporaryDirectory() as tmp:
        test_sealed_log(tmp)
    with tempfile.TemporaryDirectory() as tmp:
        test_scheduler(tmp)
    with tempfile.TemporaryDirectory() as tmp:
        test_run_due_cli(tmp)
    passed = sum(1 for _, ok in results if ok)
    print("SUMMARY: %d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

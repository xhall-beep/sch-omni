#!/usr/bin/env python3
"""SCH Omni orchestration core — pipelines, sealed log, scheduler.

The original SCH Omni design reached for an "omni system": a named-action
backend that could chain work, keep an integrity-sealed record of what it
did, and run things on a schedule. Its integrity hash shipped as a
zero-length placeholder — the intent was there, the implementation
wasn't. This module is that implementation, in pure Python 3 stdlib:

- ``SealedLog``   — append-only JSONL log where every entry carries a
  SHA-256 seal chained to the previous entry's seal (the same pattern
  as the ApexYX mesh bus). ``verify()`` re-walks the chain and reports
  the first tampered/broken entry, if any.
- Pipelines       — named multi-step workflows over the action registry.
  Each step's params may reference earlier outputs with ``{path}``
  templates resolved against a context of ``{input, prev, steps}``;
  every run produces a run record with per-step resolved params,
  results, and errors. A failing step stops the pipeline.
- ``ScheduleStore`` + ``run_due`` — a JSON-on-disk schedule store for
  interval and one-shot actions/pipelines, plus a due-runner suitable
  for cron (``server.py --run-due``) or an in-process tick thread.
  Schedules live in the file, so they survive restarts.

Nothing here knows about HTTP or the estate: an *executor* callable
``executor(action_name, params) -> result`` (raising on failure) is
injected by the caller. That keeps every piece hermetically testable.
"""

import hashlib
import json
import os
import re
import threading
import time
import uuid

GENESIS_SEAL = "0" * 64


# ---------------------------------------------------------------- sealed log

def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _seal_for(prev_seal, entry_without_seal):
    payload = prev_seal + _canonical(entry_without_seal)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SealedLog:
    """Append-only, hash-chained execution log (JSON lines)."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._last_seal = None
        self._next_seq = None

    # -- internals ------------------------------------------------------
    def _ensure_state(self):
        if self._next_seq is not None:
            return
        self._last_seal = GENESIS_SEAL
        self._next_seq = 0
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        break  # torn tail write; verify() will report it
                    self._last_seal = entry.get("seal", self._last_seal)
                    self._next_seq = int(entry.get("seq", self._next_seq)) + 1

    # -- public API -----------------------------------------------------
    def append(self, kind, name, ok, meta=None):
        """Append one sealed entry; returns the entry dict."""
        with self._lock:
            self._ensure_state()
            entry = {
                "seq": self._next_seq,
                "ts": time.time(),
                "kind": kind,
                "name": name,
                "ok": bool(ok),
                "meta": meta or {},
                "prev": self._last_seal,
            }
            entry["seal"] = _seal_for(self._last_seal, entry)
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(_canonical(entry) + "\n")
            self._last_seal = entry["seal"]
            self._next_seq += 1
            return entry

    def read_entries(self):
        entries = []
        if not os.path.exists(self.path):
            return entries
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries

    def tail(self, n=10):
        entries = self.read_entries()
        return entries[-n:] if n > 0 else entries

    def verify(self):
        """Re-walk the chain. Returns a report dict; ``ok`` is True only
        if every entry parses, seqs are continuous, prev-links match,
        and every seal recomputes exactly."""
        report = {"ok": True, "entries": 0, "first_bad_seq": None,
                  "reason": None}
        if not os.path.exists(self.path):
            return report
        prev = GENESIS_SEAL
        expected_seq = 0
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    report.update(ok=False, first_bad_seq=expected_seq,
                                  reason="unparseable entry")
                    return report
                seq = entry.get("seq")
                if seq != expected_seq:
                    report.update(ok=False, first_bad_seq=seq,
                                  reason="seq gap: expected %d" % expected_seq)
                    return report
                if entry.get("prev") != prev:
                    report.update(ok=False, first_bad_seq=seq,
                                  reason="prev link mismatch")
                    return report
                body = {k: v for k, v in entry.items() if k != "seal"}
                if _seal_for(prev, body) != entry.get("seal"):
                    report.update(ok=False, first_bad_seq=seq,
                                  reason="seal mismatch (entry altered)")
                    return report
                prev = entry["seal"]
                expected_seq += 1
                report["entries"] += 1
        return report


# ---------------------------------------------------------------- pipelines

_TEMPLATE_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")


class TemplateError(Exception):
    pass


def _resolve_path(path, context):
    node = context
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() \
                and int(part) < len(node):
            node = node[int(part)]
        else:
            raise TemplateError("cannot resolve {%s}: no key %r"
                                % (path, part))
    return node


def render_templates(value, context):
    """Deep-render ``{path}`` templates in params against the context.

    A string that is exactly one template yields the referenced value
    with its native type preserved; templates embedded in a longer
    string are string-substituted. Dicts and lists are recursed.
    """
    if isinstance(value, str):
        whole = _TEMPLATE_RE.fullmatch(value.strip())
        if whole:
            return _resolve_path(whole.group(1), context)
        def _sub(match):
            resolved = _resolve_path(match.group(1), context)
            return resolved if isinstance(resolved, str) else _canonical(resolved)
        return _TEMPLATE_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: render_templates(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render_templates(v, context) for v in value]
    return value


# A pipeline is a list of steps: {"action": name, "params": {...},
# "save_as": optional context key}. Steps run in order; each step's
# result lands in context["steps"][save_as or action] and in
# context["prev"] for the next step.
PIPELINES = {
    "estate_snapshot": {
        "description": "Claims ledger, SKU freeze state, releases, hub "
                       "health — one estate snapshot, each step fed the "
                       "previous steps' outputs.",
        "steps": [
            {"action": "claims_today", "save_as": "claims", "params": {}},
            {"action": "sku_count", "save_as": "skus",
             "params": {"ledger_date": "{steps.claims.date}",
                        "claims_latest": "{steps.claims.claims}"}},
            {"action": "releases_next", "save_as": "releases",
             "params": {"sku_count_seen": "{steps.skus.sku_count}",
                        "freeze_limit_seen": "{steps.skus.freeze_limit}"}},
            {"action": "hub_status", "save_as": "hub", "params": {}},
        ],
    },
    "freeze_audit": {
        "description": "SKU count first, then the claims ledger annotated "
                       "with the freeze numbers it was checked against.",
        "steps": [
            {"action": "sku_count", "save_as": "skus", "params": {}},
            {"action": "claims_today", "save_as": "claims",
             "params": {"sku_count_seen": "{steps.skus.sku_count}",
                        "freeze_limit_seen": "{steps.skus.freeze_limit}"}},
        ],
    },
}


def list_pipelines():
    return {
        name: {"description": spec.get("description", ""),
               "steps": [s["action"] for s in spec["steps"]]}
        for name, spec in PIPELINES.items()
    }


def run_pipeline(name, params, executor, logger=None):
    """Run a named pipeline. ``executor(action, params)`` performs one
    action and raises on failure. ``logger(kind, name, ok, meta)`` — if
    given — receives one sealed-log callback for the run as a whole.
    Returns the run record; never raises for step failures (they are in
    the record), but raises KeyError for an unknown pipeline name."""
    spec = PIPELINES.get(name)
    if spec is None:
        raise KeyError("unknown pipeline: %s" % name)
    params = params or {}
    record = {
        "pipeline": name,
        "started_at": time.time(),
        "finished_at": None,
        "ok": True,
        "input": params,
        "steps": [],
    }
    context = {"input": params, "prev": None, "steps": {}}
    for step in spec["steps"]:
        action = step["action"]
        save_as = step.get("save_as") or action
        step_rec = {"action": action, "save_as": save_as}
        try:
            resolved = render_templates(step.get("params") or {}, context)
            step_rec["params"] = resolved
            result = executor(action, resolved)
        except Exception as exc:
            step_rec.update(ok=False, error=str(exc))
            record["steps"].append(step_rec)
            record["ok"] = False
            break
        step_rec.update(ok=True, result=result)
        record["steps"].append(step_rec)
        context["steps"][save_as] = result
        context["prev"] = result
    record["finished_at"] = time.time()
    record["context"] = {"steps": context["steps"]}
    if logger is not None:
        try:
            logger("pipeline", name, record["ok"],
                   {"steps": len(record["steps"])})
        except Exception:
            pass  # logging must never break a run
    return record


# ---------------------------------------------------------------- scheduler

class ScheduleStore:
    """JSON-on-disk schedule store. Entries:

    {id, kind: "interval"|"once", target_type: "action"|"pipeline",
     target, params, every_seconds (interval) | run_at (once, epoch),
     enabled, next_run (epoch), last_run, run_count, created_at}
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    # -- persistence ----------------------------------------------------
    def _load(self):
        if not os.path.exists(self.path):
            return {"version": 1, "schedules": []}
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("version", 1)
        data.setdefault("schedules", [])
        return data

    def _save(self, data):
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    # -- public API -----------------------------------------------------
    def list(self):
        with self._lock:
            return list(self._load()["schedules"])

    def get(self, schedule_id):
        for entry in self.list():
            if entry["id"] == schedule_id:
                return entry
        return None

    def add(self, kind, target_type, target, params=None, *,
            every_seconds=None, run_at=None, now=None):
        if kind not in ("interval", "once"):
            raise ValueError("kind must be 'interval' or 'once'")
        if target_type not in ("action", "pipeline"):
            raise ValueError("target_type must be 'action' or 'pipeline'")
        now = time.time() if now is None else now
        entry = {
            "id": uuid.uuid4().hex[:12],
            "kind": kind,
            "target_type": target_type,
            "target": target,
            "params": params or {},
            "enabled": True,
            "last_run": None,
            "run_count": 0,
            "created_at": now,
        }
        if kind == "interval":
            every = int(every_seconds or 0)
            if every <= 0:
                raise ValueError("interval schedules need every_seconds > 0")
            entry["every_seconds"] = every
            entry["next_run"] = now + every
        else:
            if run_at is None:
                raise ValueError("one-shot schedules need run_at (epoch)")
            entry["run_at"] = float(run_at)
            entry["next_run"] = float(run_at)
        with self._lock:
            data = self._load()
            data["schedules"].append(entry)
            self._save(data)
        return entry

    def remove(self, schedule_id):
        with self._lock:
            data = self._load()
            before = len(data["schedules"])
            data["schedules"] = [s for s in data["schedules"]
                                 if s["id"] != schedule_id]
            if len(data["schedules"]) == before:
                return False
            self._save(data)
            return True

    def due(self, now=None):
        now = time.time() if now is None else now
        return [s for s in self.list()
                if s.get("enabled") and s.get("next_run") is not None
                and s["next_run"] <= now]

    def mark_run(self, schedule_id, now):
        """Advance an entry after it fired: intervals roll forward on
        their cadence (missed firings are skipped, never burst), and a
        one-shot disables itself so it can never refire."""
        with self._lock:
            data = self._load()
            for entry in data["schedules"]:
                if entry["id"] != schedule_id:
                    continue
                entry["last_run"] = now
                entry["run_count"] = int(entry.get("run_count", 0)) + 1
                if entry["kind"] == "interval":
                    every = int(entry["every_seconds"])
                    nxt = float(entry["next_run"]) + every
                    while nxt <= now:
                        nxt += every
                    entry["next_run"] = nxt
                else:
                    entry["enabled"] = False
                    entry["fired_at"] = now
                self._save(data)
                return entry
            return None


def run_due(store, dispatch, now=None):
    """Fire every due schedule exactly once. ``dispatch(target_type,
    target, params)`` performs the work and raises on failure. Returns
    one run record per fired schedule; a failed firing is recorded and
    still advances the schedule (no hot retry loop)."""
    now = time.time() if now is None else now
    records = []
    for entry in store.due(now):
        record = {"schedule_id": entry["id"], "target": entry["target"],
                  "target_type": entry["target_type"], "ran_at": now}
        try:
            result = dispatch(entry["target_type"], entry["target"],
                              entry.get("params") or {})
            record.update(ok=True, result=result)
        except Exception as exc:
            record.update(ok=False, error=str(exc))
        store.mark_run(entry["id"], now)
        records.append(record)
    return records

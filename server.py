#!/usr/bin/env python3
"""SCH Omni action router — the honest rebuild.

The original sch-omni was a Flask stub: every handler returned
{"status": "stub"} and the file could not even be imported. This is
the "SCH Omni System" — the estate's omni orchestrator. This version
is a small, dependency-free (Python 3 stdlib only) HTTP server whose
actions are wired to live estate data, and which grows the router into
the orchestrator the design pointed at:

- action pipelines: named multi-step workflows chaining actions, each
  step's output feeding the next step's params (see omni.PIPELINES);
- a sealed action log: every execution appended to a SHA-256
  hash-chained log, verifiable end-to-end (the original design's
  integrity hash, implemented for real this time);
- a persisted scheduler: interval and one-shot actions/pipelines in a
  JSON store on disk, fired by `python3 server.py --run-due` (cron) or
  the server's in-process tick thread; schedules survive restarts.

No stubs, no hardcoded results: if a data source is unreachable, the
action says so.

Usage:
    python3 server.py [--port N] [--host H] [--tick-seconds N]
    python3 server.py --run-due     # fire due schedules once, then exit

State (sealed log + schedules) lives in $SCH_OMNI_STATE_DIR, defaulting
to ./state next to this file.

API:
    GET  /health      -> 200 {"ok": true}                       (no auth)
    POST /api/action  -> Authorization: Bearer <token>
         body: {"action": "<name>", "params": {...}}
         200 {"ok": true, "action": ..., "result": ...}
         400 malformed JSON body / missing action
         401 missing or wrong token
         404 unknown action
         502 the action's live data source failed (error text included)

Token: read from .token in this directory. If absent, a random token is
generated and written with mode 0600. The token value is never logged.
"""

import argparse
import csv
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import omni

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(HERE, ".token")
WORKSPACE = os.path.expanduser("~/workspace")
SSH_MESH = os.path.join(WORKSPACE, "bin", "ssh-mesh")
CLAIMS_CSV = os.path.join(WORKSPACE, "capability-stack", "claims.csv")
SKU_MAP_JSON = os.path.join(WORKSPACE, "capability-stack", "sku-map.json")
RELEASES_DIR = os.path.join(WORKSPACE, "capability-stack", "releases")


def load_or_create_token():
    """Return the router token, generating and persisting one if absent."""
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
        if token:
            return token
    token = secrets.token_hex(32)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return token


# ---------------------------------------------------------------- actions

def action_hub_status(params):
    """Live health of the Pixel estate hub (the epoch-2 primary).

    The hub token is read ON the Pixel from its own config file and used
    in the curl there; it is never transported to this machine.
    """
    remote = (
        'T=$(cat ~/.config/omni-mesh/hub-token 2>/dev/null); '
        'if [ -n "$T" ]; then '
        'curl -s -m 15 -H "Authorization: Bearer $T" http://127.0.0.1:8471/health; '
        'else curl -s -m 15 http://127.0.0.1:8471/health; fi'
    )
    proc = subprocess.run(
        [SSH_MESH, "pixel", remote],
        capture_output=True, text=True, timeout=90,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "ssh-mesh pixel failed (rc=%d): %s"
            % (proc.returncode, (proc.stderr or proc.stdout).strip()[:300])
        )
    try:
        health = json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError(
            "hub /health returned non-JSON: %r" % proc.stdout.strip()[:200]
        )
    return {"hub": "pixel", "health": health}


def action_claims_today(params):
    """Latest row of the verified-claims ledger (claims.csv)."""
    with open(CLAIMS_CSV, "r", encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("date")]
    if not rows:
        raise RuntimeError("claims.csv has no data rows")
    latest = rows[-1]
    raw_count = latest.get("verified_claims_total", "")
    try:
        claims = int(raw_count)
    except (TypeError, ValueError):
        claims = raw_count
    return {
        "date": latest.get("date"),
        "claims": claims,
        "notes": latest.get("source", ""),
        "rows_total": len(rows),
    }


def action_sku_count(params):
    """SKU count from sku-map.json, plus the freeze limit if stated."""
    with open(SKU_MAP_JSON, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    products = data.get("products", [])
    annex = data.get("annex_off_storefront", [])
    freeze_text = data.get("freeze", "")
    limit = None
    match = re.search(r"\d+", freeze_text or "")
    if match:
        limit = int(match.group(0))
    return {
        "sku_count": len(products),
        "annex_off_storefront": len(annex),
        "freeze_limit": limit,
        "freeze": freeze_text,
        "as_of": data.get("as_of"),
    }


def action_releases_next(params):
    """Files in the release-packet directory, with sizes."""
    entries = []
    for entry in sorted(os.scandir(RELEASES_DIR), key=lambda e: e.name):
        st = entry.stat()
        entries.append({
            "name": entry.name,
            "size_bytes": st.st_size,
            "is_dir": entry.is_dir(),
        })
    return {"dir": "capability-stack/releases", "files": entries,
            "count": len(entries)}


ACTIONS = {
    "hub_status": action_hub_status,
    "claims_today": action_claims_today,
    "sku_count": action_sku_count,
    "releases_next": action_releases_next,
}


# ------------------------------------------------------- orchestration
# Pipelines, the sealed action log, and the persisted scheduler live in
# omni.py; this section wires them to the action registry. Every
# execution — direct, pipeline step, or scheduled — is appended to the
# sealed log. State dir: $SCH_OMNI_STATE_DIR or ./state beside this file.

STATE_DIR = os.environ.get(
    "SCH_OMNI_STATE_DIR", os.path.join(HERE, "state"))
SEALED_LOG = omni.SealedLog(os.path.join(STATE_DIR, "action-log.jsonl"))
SCHEDULES = omni.ScheduleStore(os.path.join(STATE_DIR, "schedules.json"))

# Actions a pipeline step may not invoke (recursion / meta control).
_META_ACTIONS = {"pipeline_run", "schedule_run_due"}


class UnknownPipelineError(Exception):
    """pipeline_run named a pipeline that does not exist (-> HTTP 404)."""


def _seal(kind, name, ok, meta=None):
    try:
        SEALED_LOG.append(kind, name, ok, meta)
    except Exception:
        pass  # the log must never break an execution


def dispatch(action, params, kind="action"):
    """Run one registered action and seal the execution."""
    handler = ACTIONS.get(action)
    if handler is None:
        _seal(kind, action, False, {"error": "unknown action"})
        raise KeyError("unknown action: %s" % action)
    try:
        result = handler(params or {})
    except Exception as exc:
        _seal(kind, action, False, {"error": str(exc)[:200]})
        raise
    _seal(kind, action, True)
    return result


def _leaf_dispatch(action, params):
    """Executor for pipeline steps: leaf actions only, no meta calls."""
    if action in _META_ACTIONS:
        raise RuntimeError("pipeline steps may not invoke %s" % action)
    return dispatch(action, params, kind="step")


def _scheduled_dispatch(target_type, target, params):
    if target_type == "pipeline":
        return omni.run_pipeline(
            target, params, _leaf_dispatch,
            logger=lambda k, n, ok, meta: _seal(k, n, ok, meta))
    return dispatch(target, params, kind="scheduled")


def action_pipeline_list(params):
    return {"pipelines": omni.list_pipelines()}


def action_pipeline_run(params):
    name = params.get("name")
    if not name:
        raise ValueError("pipeline_run needs params.name")
    if name not in omni.PIPELINES:
        raise UnknownPipelineError("unknown pipeline: %s" % name)
    return omni.run_pipeline(
        name, params.get("params") or {}, _leaf_dispatch,
        logger=lambda k, n, ok, meta: _seal(k, n, ok, meta))


def action_log_verify(params):
    return SEALED_LOG.verify()


def action_log_tail(params):
    try:
        n = int(params.get("n", 10))
    except (TypeError, ValueError):
        n = 10
    entries = SEALED_LOG.tail(max(1, min(n, 200)))
    return {"entries": [
        {"seq": e["seq"], "ts": e["ts"], "kind": e["kind"],
         "name": e["name"], "ok": e["ok"], "seal": e["seal"][:12]}
        for e in entries]}


def action_schedule_add(params):
    kind = params.get("kind")
    target_type = params.get("target_type", "action")
    target = params.get("target")
    if not target:
        raise ValueError("schedule_add needs params.target")
    entry = SCHEDULES.add(
        kind, target_type, target, params.get("params") or {},
        every_seconds=params.get("every_seconds"),
        run_at=params.get("run_at"))
    return {"schedule": entry}


def action_schedule_list(params):
    return {"schedules": SCHEDULES.list()}


def action_schedule_remove(params):
    schedule_id = params.get("id")
    if not schedule_id:
        raise ValueError("schedule_remove needs params.id")
    return {"removed": SCHEDULES.remove(schedule_id)}


def action_schedule_run_due(params):
    records = omni.run_due(SCHEDULES, _scheduled_dispatch)
    return {"ran": records, "count": len(records)}


ACTIONS.update({
    "pipeline_list": action_pipeline_list,
    "pipeline_run": action_pipeline_run,
    "log_verify": action_log_verify,
    "log_tail": action_log_tail,
    "schedule_add": action_schedule_add,
    "schedule_list": action_schedule_list,
    "schedule_remove": action_schedule_remove,
    "schedule_run_due": action_schedule_run_due,
})


def _tick_loop(interval_seconds):
    while True:
        time.sleep(interval_seconds)
        try:
            omni.run_due(SCHEDULES, _scheduled_dispatch)
        except Exception:
            pass  # a tick failure must never kill the server


# ---------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    token = None  # set in main()
    protocol_version = "HTTP/1.1"

    # Quiet logging: method + path + status only, never headers/token.
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        presented = header[len("Bearer "):].strip()
        return hmac.compare_digest(presented, self.token)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/api/action":
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        if not self._authorized():
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"ok": False, "error": "malformed JSON body"})
            return
        action = body.get("action") if isinstance(body, dict) else None
        if not action or not isinstance(action, str):
            self._send_json(400, {"ok": False, "error": "missing 'action'"})
            return
        params = body.get("params") or {}
        if action not in ACTIONS:
            _seal("action", action, False, {"error": "unknown action"})
            self._send_json(404, {"ok": False,
                                  "error": "unknown action: %s" % action})
            return
        try:
            result = dispatch(action, params, kind="action")
        except UnknownPipelineError as exc:
            self._send_json(404, {"ok": False, "action": action,
                                  "error": str(exc)})
            return
        except Exception as exc:  # live source failed — say so, don't fake it
            self._send_json(502, {"ok": False, "action": action,
                                  "error": str(exc)})
            return
        self._send_json(200, {"ok": True, "action": action, "result": result})


def main():
    parser = argparse.ArgumentParser(description="SCH Omni orchestrator")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--tick-seconds", type=int, default=30,
                        help="in-process scheduler tick (0 disables)")
    parser.add_argument("--run-due", action="store_true",
                        help="fire due schedules once, print JSON, exit")
    args = parser.parse_args()

    if args.run_due:
        records = omni.run_due(SCHEDULES, _scheduled_dispatch)
        print(json.dumps({"ran": records, "count": len(records)},
                         indent=2))
        return 0

    Handler.token = load_or_create_token()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    if args.tick_seconds > 0:
        tick = threading.Thread(target=_tick_loop,
                                args=(args.tick_seconds,), daemon=True)
        tick.start()
    sys.stderr.write(
        "sch-omni listening on %s:%d (token auth on /api/action, "
        "scheduler tick %ss, state dir %s)\n"
        % (args.host, args.port, args.tick_seconds, STATE_DIR)
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())

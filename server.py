#!/usr/bin/env python3
"""SCH Omni action router — the honest rebuild.

The original sch-omni was a Flask stub: every handler returned
{"status": "stub"} and the file could not even be imported. This version
is a small, dependency-free (Python 3 stdlib only) HTTP server whose
actions are wired to live estate data. No stubs, no hardcoded results:
if a data source is unreachable, the action says so.

Usage:
    python3 server.py [--port N] [--host H]

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
        handler = ACTIONS.get(action)
        if handler is None:
            self._send_json(404, {"ok": False,
                                  "error": "unknown action: %s" % action})
            return
        try:
            result = handler(params)
        except Exception as exc:  # live source failed — say so, don't fake it
            self._send_json(502, {"ok": False, "action": action,
                                  "error": str(exc)})
            return
        self._send_json(200, {"ok": True, "action": action, "result": result})


def main():
    parser = argparse.ArgumentParser(description="SCH Omni action router")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    Handler.token = load_or_create_token()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write(
        "sch-omni-router listening on %s:%d (token auth on /api/action)\n"
        % (args.host, args.port)
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

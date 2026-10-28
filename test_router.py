#!/usr/bin/env python3
"""End-to-end test for the SCH Omni action router.

Starts server.py on an ephemeral port, exercises all four actions with
the token, the three error behaviours, and the router's own /health.
Prints one PASS/FAIL line per check plus a final summary; exits
non-zero if any check fails. The token is read from .token but never
printed.
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(HERE, ".token")

results = []


def check(name, condition, detail=""):
    results.append((name, bool(condition)))
    line = "%s %s" % ("PASS" if condition else "FAIL", name)
    if detail:
        line += " — " + detail
    print(line, flush=True)


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def request(method, url, body=None, token=None, raw_body=None, timeout=120):
    data = raw_body
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        payload = err.read().decode("utf-8")
        try:
            return err.code, json.loads(payload)
        except ValueError:
            return err.code, {"raw": payload}


def wait_for_health(base, proc, timeout_s=20):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            status, _ = request("GET", base + "/health", timeout=5)
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def main():
    port = free_port()
    base = "http://127.0.0.1:%d" % port
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "server.py"),
         "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_for_health(base, proc):
            check("server starts and answers /health", False,
                  "server did not come up")
            return finish()

        check("server starts and answers /health", True)

        with open(TOKEN_PATH, "r", encoding="utf-8") as fh:
            token = fh.read().strip()

        # Router health, exact shape
        status, payload = request("GET", base + "/health")
        check("GET /health -> 200 {\"ok\": true}",
              status == 200 and payload.get("ok") is True,
              "status=%d" % status)

        # Action 1: hub_status (live, via ssh-mesh to the Pixel hub)
        status, payload = request(
            "POST", base + "/api/action",
            body={"action": "hub_status", "params": {}}, token=token)
        hub = (payload.get("result") or {}).get("health") or {}
        check("action hub_status -> live Pixel hub health",
              status == 200 and payload.get("ok") is True
              and hub.get("ok") is True and "version" in hub,
              "status=%d hub_ok=%s version=%s"
              % (status, hub.get("ok"), hub.get("version")))

        # Action 2: claims_today (latest claims.csv row)
        status, payload = request(
            "POST", base + "/api/action",
            body={"action": "claims_today", "params": {}}, token=token)
        res = payload.get("result") or {}
        # Compare against the ledger's actual latest row. (The original
        # check hardcoded the 2026-10-27 row and rotted the next day.)
        import csv as _csv
        with open(os.path.expanduser(
                "~/workspace/capability-stack/claims.csv"),
                newline="", encoding="utf-8") as _fh:
            _rows = [r for r in _csv.DictReader(_fh) if r.get("date")]
        _latest = _rows[-1] if _rows else {}
        check("action claims_today -> latest ledger row",
              status == 200 and payload.get("ok") is True
              and res.get("date") == _latest.get("date")
              and res.get("claims")
              == int(_latest.get("verified_claims_total", -1))
              and "notes" in res,
              "status=%d date=%s claims=%s"
              % (status, res.get("date"), res.get("claims")))

        # Action 3: sku_count (sku-map.json)
        status, payload = request(
            "POST", base + "/api/action",
            body={"action": "sku_count", "params": {}}, token=token)
        res = payload.get("result") or {}
        check("action sku_count -> 22 SKUs, freeze limit 22",
              status == 200 and payload.get("ok") is True
              and res.get("sku_count") == 22
              and res.get("freeze_limit") == 22,
              "status=%d sku_count=%s freeze_limit=%s"
              % (status, res.get("sku_count"), res.get("freeze_limit")))

        # Action 4: releases_next (releases dir listing)
        status, payload = request(
            "POST", base + "/api/action",
            body={"action": "releases_next", "params": {}}, token=token)
        res = payload.get("result") or {}
        names = [f.get("name") for f in res.get("files", [])]
        sizes = {f.get("name"): f.get("size_bytes")
                 for f in res.get("files", [])}
        check("action releases_next -> release files with sizes",
              status == 200 and payload.get("ok") is True
              and "deep-honey-trap.md" in names
              and all(isinstance(s, int) and s > 0
                      for s in sizes.values()),
              "status=%d files=%s" % (status, names))

        # Error case 1: unknown action -> 404
        status, payload = request(
            "POST", base + "/api/action",
            body={"action": "no_such_action", "params": {}}, token=token)
        check("unknown action -> 404",
              status == 404 and payload.get("ok") is False,
              "status=%d" % status)

        # Error case 2a: missing token -> 401
        status, payload = request(
            "POST", base + "/api/action",
            body={"action": "claims_today", "params": {}})
        check("missing token -> 401",
              status == 401 and payload.get("ok") is False,
              "status=%d" % status)

        # Error case 2b: wrong token -> 401
        status, payload = request(
            "POST", base + "/api/action",
            body={"action": "claims_today", "params": {}},
            token="definitely-not-the-token")
        check("wrong token -> 401",
              status == 401 and payload.get("ok") is False,
              "status=%d" % status)

        # Error case 3: malformed JSON body -> 400
        status, payload = request(
            "POST", base + "/api/action", token=token,
            raw_body=b"{this is not json")
        check("malformed JSON body -> 400",
              status == 400 and payload.get("ok") is False,
              "status=%d" % status)

        return finish()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def finish():
    passed = sum(1 for _, ok in results if ok)
    print("SUMMARY: %d/%d checks passed" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

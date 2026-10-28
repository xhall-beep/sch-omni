# SCH Omni — Action Router

**Upgraded 2026-10-28.** This repo previously held a Flask stub whose
handlers all returned `{"status": "stub"}` — and whose `app.py` could
not even be imported (a chat-share URL was glued onto its first line).
That scaffold is superseded by the working router below (the old files
were removed in this upgrade; they remain in git history). This rebuild
keeps the one sound idea — a tiny named-action HTTP router — and wires
every action to live estate data. No stubs, no hardcoded results: if a
source is down, the action returns an error saying so.

Dependency-free: Python 3 standard library only (`http.server`). $0 to run.

## Run

```bash
python3 server.py --port 8799        # binds 127.0.0.1 by default
python3 server.py --port 8799 --host 0.0.0.0
```

## API

- `GET /health` → `200 {"ok": true}` — the router itself. No auth.
- `POST /api/action` — header `Authorization: Bearer <token>`,
  body `{"action": "<name>", "params": {...}}`.
  - `200 {"ok": true, "action": ..., "result": ...}` on success
  - `400` malformed JSON body / missing `action`
  - `401` missing or wrong token
  - `404` unknown action
  - `502` the action's live data source failed (error text in the body)

## Actions (all read-only, all live)

| Action | What it does |
|---|---|
| `hub_status` | SSHes to the Pixel via `~/workspace/bin/ssh-mesh` and returns the estate hub's `/health` JSON. The hub token is read on the Pixel from its own `~/.config/omni-mesh/hub-token` and never leaves that node. |
| `claims_today` | Latest row of `~/workspace/capability-stack/claims.csv`: date, verified claims count, source notes. |
| `sku_count` | Counts SKUs in `~/workspace/capability-stack/sku-map.json` (storefront products + annex), and reports the freeze limit stated in the file. |
| `releases_next` | Lists `~/workspace/capability-stack/releases/` with file names and sizes. |

## Token

On first start the server creates `.token` in this directory with a
random value, file mode `0600`. Send it as `Authorization: Bearer
<token>`. To rotate: delete `.token` and restart — a new one is
generated. The token is never printed in logs or test output.

## Test

```bash
python3 test_router.py
```

Starts the server on an ephemeral port, exercises all four actions
against the live sources, all three error behaviours (404 / 401 / 400),
and `/health`; prints one PASS/FAIL line per check and exits non-zero
on any failure.

## Latest verification

2026-10-28, in this repo, on the estate VM: `python3 test_router.py`
→ **10/10 checks passed** (all four actions against the live sources,
404/401/400 behaviours, and `/health`). One test repair ships with this
upgrade: the claims check originally hardcoded the 2026-10-27 ledger
row and rotted the next day; it now compares against the ledger's
actual latest row.

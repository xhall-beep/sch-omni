# SCH Omni — the Omni Orchestrator

**Upgraded 2026-10-28, vision build same day.** This repo previously
held a Flask stub whose handlers all returned `{"status": "stub"}` —
and whose `app.py` could not even be imported (a chat-share URL was
glued onto its first line). The first upgrade replaced it with a
working named-action router wired to live estate data. The vision
build grows that router into the **SCH Omni System** the original
design pointed at — an omni *orchestrator*, not just a request router:

- **Action pipelines** — named multi-step workflows that chain actions,
  each step's output feeding the next step's params.
- **A sealed action log** — every execution appended to a SHA-256
  hash-chained log. The original design shipped a zero-length
  integrity hash; the seal is implemented for real here, with a verify
  action that re-walks the chain and names the first tampered entry.
- **A persisted scheduler** — interval and one-shot actions/pipelines
  in a JSON store on disk, fired by cron (`--run-due`) or the server's
  in-process tick; schedules survive restarts.

No stubs, no hardcoded results: if a source is down, the action
returns an error saying so.

Dependency-free: Python 3 standard library only. $0 to run.

## Run

```bash
python3 server.py --port 8799        # binds 127.0.0.1 by default
python3 server.py --port 8799 --host 0.0.0.0
python3 server.py --run-due          # fire due schedules once, exit (cron)
```

State (sealed log + schedule store) lives in `$SCH_OMNI_STATE_DIR`,
default `./state/` beside `server.py`.

## API

- `GET /health` → `200 {"ok": true}` — the router itself. No auth.
- `POST /api/action` — header `Authorization: Bearer <token>`,
  body `{"action": "<name>", "params": {...}}`.
  - `200 {"ok": true, "action": ..., "result": ...}` on success
  - `400` malformed JSON body / missing `action`
  - `401` missing or wrong token
  - `404` unknown action
  - `502` the action's live data source failed (error text in the body)

## Actions — leaf (all read-only, all live)

| Action | What it does |
|---|---|
| `hub_status` | SSHes to the Pixel via `~/workspace/bin/ssh-mesh` and returns the estate hub's `/health` JSON. The hub token is read on the Pixel from its own `~/.config/omni-mesh/hub-token` and never leaves that node. |
| `claims_today` | Latest row of `~/workspace/capability-stack/claims.csv`: date, verified claims count, source notes. |
| `sku_count` | Counts SKUs in `~/workspace/capability-stack/sku-map.json` (storefront products + annex), and reports the freeze limit stated in the file. |
| `releases_next` | Lists `~/workspace/capability-stack/releases/` with file names and sizes. |

## Actions — orchestration

| Action | What it does |
|---|---|
| `pipeline_list` | The named pipelines and their step sequences. |
| `pipeline_run` | Runs one pipeline: `{"name": ..., "params": {...}}`. Returns the run record (per-step resolved params, results, errors). 404 for an unknown pipeline name. |
| `log_verify` | Re-walks the sealed action log: `{ok, entries, first_bad_seq, reason}`. |
| `log_tail` | Last N log entries (`{"n": 10}`): seq, time, kind, name, ok, seal prefix. |
| `schedule_add` | Adds a schedule: `{"kind": "interval", "every_seconds": N, ...}` or `{"kind": "once", "run_at": <epoch>, ...}`, plus `target_type` (`action`/`pipeline`), `target`, `params`. |
| `schedule_list` | All persisted schedules with next-run times and run counts. |
| `schedule_remove` | Removes one schedule by `{"id": ...}`. |
| `schedule_run_due` | Fires every due schedule once and returns the run records. |

### Pipelines

Step params may reference earlier outputs with `{path}` templates
resolved against `{input, prev, steps.<save_as>}`; a template that is
the whole string keeps its native type (a number stays a number).
Steps cannot invoke `pipeline_run` or `schedule_run_due`. A failing
step stops the pipeline and the failure lands in the run record.
Built in:

- `estate_snapshot` — claims → sku_count (fed the ledger date/count) →
  releases_next (fed the SKU/freeze numbers) → hub_status.
- `freeze_audit` — sku_count → claims_today annotated with the freeze
  numbers it was checked against.

### Sealed action log

`state/action-log.jsonl`: one JSON entry per execution (direct action,
pipeline step, pipeline run, scheduled firing) with `seq`, timestamp,
kind, name, ok, a small meta, the previous entry's seal, and its own
SHA-256 seal over `prev + canonical(entry)`. This is the ApexYX bus
seal pattern. `log_verify` detects edits, insertions, and gaps.

### Scheduler

`state/schedules.json`, written atomically. Interval entries roll
forward on cadence (missed firings are skipped, never burst); one-shots
disable themselves after firing. Fire them via the in-process tick
(`--tick-seconds`, default 30), the `schedule_run_due` action, or cron:

```bash
* * * * * cd /path/to/sch-omni && python3 server.py --run-due
```

## Token

On first start the server creates `.token` in this directory with a
random value, file mode `0600`. Send it as `Authorization: Bearer
<token>`. To rotate: delete `.token` and restart — a new one is
generated. The token is never printed in logs or test output.

## Test

```bash
python3 test_router.py   # live end-to-end: the four leaf actions,
                         # error behaviours, /health — 10 checks
python3 test_omni.py     # hermetic: pipelines, sealed log, scheduler,
                         # --run-due CLI (temp dirs, no live sources)
```

`test_router.py` starts the server on an ephemeral port, exercises all
four leaf actions against the live sources, all three error behaviours
(404 / 401 / 400), and `/health`; prints one PASS/FAIL line per check
and exits non-zero on any failure. `test_omni.py` proves pipeline
data-passing end-to-end, that a tampered log entry is detected at the
right sequence number, and that a due schedule fires exactly once
(a one-shot never refires; an interval advances on cadence).

## Latest verification

2026-10-28, in this repo, on the estate VM: `python3 test_router.py`
→ **10/10 checks passed**; `python3 test_omni.py` → **14/14 checks
passed**; a live smoke run drove both built-in pipelines over HTTP
(both fully green against live sources), fired a one-shot via the
in-process tick, and verified the sealed log at 16 entries. One test
repair from the first upgrade still stands: the claims check compares
against the ledger's actual latest row instead of a hardcoded date.

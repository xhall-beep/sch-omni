# BUILD REPORT — sch-omni vision build: the SCH Omni System

**Date:** 2026-10-28 (estate VM, local workdir only — nothing pushed).
**Directive:** "Greatly improve repos. Build what's envisioned rather
than what's stated."
**Base:** the 2026-10-28 router upgrade (token-gated named-action
router, 4 live leaf actions, `test_router.py` 10/10). All of it kept
intact — actions, token handling, error codes, tests.

## The vision

The audit (Cluster A, §4) records what sch-omni was *meant* to be:
"SCH Omni System" — the backend of the sovereign-agent schema, not a
bare request router. The concept it pairs with (sovereign-vault's
concierge configs) is an always-on agent system: work chained into
workflows, an auditable record of everything it did, and jobs that
run on a schedule without a human pressing buttons. The original code
even gestured at integrity sealing — it shipped a zero-length
integrity hash. The intent was there; the implementation wasn't.

So the vision built here: **the estate's omni orchestrator** — the
router's live actions, plus (1) pipelines that chain them with real
data-passing, (2) a SHA-256 hash-chained sealed log of every
execution, verifiable end-to-end, and (3) a persisted scheduler that
fires actions/pipelines on interval or one-shot schedules, surviving
restarts, drivable from cron or an in-process tick.

## What was built

### `omni.py` (new, 429 lines) — orchestration core, HTTP-agnostic

- **`SealedLog`** — append-only JSONL log. Each entry:
  `{seq, ts, kind, name, ok, meta, prev, seal}` where
  `seal = SHA-256(prev_seal + canonical_json(entry_without_seal))`,
  genesis prev = 64 zeros — the ApexYX mesh-bus seal pattern.
  `verify()` re-walks the file checking parseability, seq continuity,
  prev linkage, and every seal; it reports `first_bad_seq` and a
  reason on the first problem. `append()` is lock-guarded and resumes
  seq/seal state from an existing file (restart-safe).
- **Pipelines** — `PIPELINES` registry of named workflows; a step is
  `{action, params, save_as}`. Params are deep-rendered against a
  context `{input, prev, steps}` with `{dotted.path}` templates; a
  whole-string template preserves the referenced value's native type,
  embedded templates string-substitute. Each run returns a run record:
  per-step resolved params, result or error, overall ok, the final
  step context. A failing step stops the pipeline; the failure is in
  the record, never raised away. Built-ins:
  - `estate_snapshot`: claims_today → sku_count (params fed the ledger
    date + claims count) → releases_next (fed sku_count + freeze
    limit) → hub_status.
  - `freeze_audit`: sku_count → claims_today annotated with the freeze
    numbers from step 1.
- **`ScheduleStore` + `run_due`** — JSON store at a caller-chosen path,
  saved atomically (tmp + `os.replace`). Entry kinds: `interval`
  (`every_seconds`) and `once` (`run_at` epoch). `due(now)` selects
  enabled, past-due entries. `run_due` fires each due entry exactly
  once through an injected dispatch callable and records the outcome;
  `mark_run` then advances intervals on cadence (missed firings are
  skipped — no catch-up bursts) and disables one-shots permanently.

### `server.py` (extended, 420 lines) — wiring

- New module import + state section. State dir from
  `$SCH_OMNI_STATE_DIR`, default `./state/` beside the file:
  `action-log.jsonl` + `schedules.json`.
- A single `dispatch(action, params, kind)` path now serves the HTTP
  handler, pipeline steps (kind `step`), and scheduled firings (kind
  `scheduled`); every execution — success, failure, even unknown
  action — is sealed into the log. Logging failures are swallowed so
  the log can never break an execution.
- Pipeline steps run through a leaf-only executor: steps may not
  invoke `pipeline_run` or `schedule_run_due` (no recursion/meta).
- **8 new token-gated actions** (same `/api/action`, same auth):
  `pipeline_list`, `pipeline_run` (unknown pipeline name → HTTP 404
  with a correct message), `log_verify`, `log_tail`,
  `schedule_add`, `schedule_list`, `schedule_remove`,
  `schedule_run_due`.
- **CLI:** `python3 server.py --run-due` fires due schedules once,
  prints the records as JSON, exits — the cron entry point.
  `--tick-seconds N` (default 30, 0 disables) runs an in-process tick
  thread doing the same while the server is up.
- The original four leaf actions, token creation/0600 handling, and
  the 200/400/401/404/502 behaviour are unchanged. `.gitignore` gained
  one line: `state/`.

### `test_omni.py` (new, 227 lines) — hermetic tests

Temp dirs + fake executors only; no live sources, no network, no
token file. 14 checks: pipeline data-passing end-to-end (step 2's
params *and* its computed result depend on step 1's output), pipeline
failure stops the run, sealed chain verifies, a tampered middle entry
is detected at seq 1 with "seal mismatch (entry altered)", schedule
store survives a disk reload, due entries fire once each, one-shot
never refires, interval advances on cadence without bursting, and the
real `server.py --run-due` CLI against a temp state dir fires a due
one-shot and persists the fired state.

### `README.md` (rewritten sections)

Title/intro now describe the orchestrator; new sections document the
orchestration actions, pipeline templating, the seal format, the
scheduler semantics, and both test suites.

## File list

| File | Status | Lines |
|---|---|---|
| `omni.py` | **new** | 429 |
| `test_omni.py` | **new** | 227 |
| `server.py` | extended (router core untouched) | 420 |
| `README.md` | updated | — |
| `.gitignore` | +1 line (`state/`) | — |
| `test_router.py` | unchanged | 212 |

## Test evidence (actual runs, 2026-10-28, this workdir)

`python3 test_omni.py` → **14/14**:

```
PASS pipeline runs end-to-end
PASS pipeline passes step-1 output into step-2 params — resolved params={'base': 41, 'note': 'label was seed'}
PASS pipeline step-2 result depends on step-1 output — value=42
PASS pipeline record carries the step context
PASS pipeline stops at the failing step
PASS sealed log chain verifies — report={'ok': True, 'entries': 3, 'first_bad_seq': None, 'reason': None}
PASS sealed log detects a tampered entry — report={'ok': False, 'entries': 1, 'first_bad_seq': 1, 'reason': 'seal mismatch (entry altered)'}
PASS scheduler store survives reload from disk
PASS scheduler fires due entries once each — records=2 fired=[('action', 'claims_today'), ('action', 'sku_count')]
PASS one-shot does not refire and interval does not burst
PASS interval advances on cadence after firing — next_run-now=3600s
PASS one-shot disables itself after firing
PASS server --run-due fires the due one-shot — rc=0 (claims_today result returned)
PASS server --run-due marks the one-shot fired on disk
SUMMARY: 14/14 checks passed
```

`python3 test_router.py` (pre-existing suite, live sources) → **10/10**,
unchanged from before the build:

```
PASS server starts and answers /health
PASS GET /health -> 200 {"ok": true} — status=200
PASS action hub_status -> live Pixel hub health — status=200 hub_ok=True version=4.8.1
PASS action claims_today -> latest ledger row — status=200 date=2026-10-28 claims=0
PASS action sku_count -> 22 SKUs, freeze limit 22 — status=200 sku_count=22 freeze_limit=22
PASS action releases_next -> release files with sizes — status=200 files=['deep-honey-trap.md', 'index.csv']
PASS unknown action -> 404 — status=404
PASS missing token -> 401 — status=401
PASS wrong token -> 401 — status=401
PASS malformed JSON body -> 400 — status=400
SUMMARY: 10/10 checks passed
```

Live HTTP smoke (throwaway driver, temp state dir, server with a 2 s
tick) — both built-in pipelines green against live sources with real
cross-step values, the tick fired a past-due one-shot on its own, and
the sealed log verified over every execution the smoke performed:

```
pipeline_list: 200 ['estate_snapshot', 'freeze_audit']
freeze_audit ok: True | steps: [('sku_count', True), ('claims_today', True)] | step2 resolved params: {'sku_count_seen': 22, 'freeze_limit_seen': 22}
estate_snapshot ok: True | steps: [('claims_today', True), ('sku_count', True), ('releases_next', True), ('hub_status', True)] | releases step saw sku_count: 22
schedule_add: 200 2bca1fab8c24
after tick: run_count: 1 enabled: False
schedule_run_due (nothing due): 0
schedule_remove: {'removed': True}
log_verify: {'ok': True, 'entries': 16, 'first_bad_seq': None, 'reason': None}
unknown pipeline -> http 404 unknown pipeline: nope
```

One defect was caught and fixed during the smoke: an unknown pipeline
name initially surfaced as 404 with the wrong message ("unknown
action: pipeline_run") because `run_pipeline`'s `KeyError` collided
with the handler's unknown-action branch. `pipeline_run` now validates
the name first and raises a dedicated `UnknownPipelineError` → 404
"unknown pipeline: <name>" (shown in the final smoke line above).

## Still missing, and why

- **Cron is documented, not installed.** Wiring a real crontab entry
  on the estate VM is an estate change outside this repo build; the
  `--run-due` entry point and a sample crontab line are in the README.
- **No schedule editing over HTTP** (only add/list/remove). Deliberate:
  remove + re-add covers it without a partial-update semantics debate.
- **Pipelines are code-defined, not data-defined.** A JSON pipeline
  store would let schedules compose new workflows at runtime, but it
  also turns stored data into executable workflow definitions; keeping
  pipelines in reviewed code is the safer v1. The engine already takes
  specs as plain dicts, so this is a small future step.
- **The sealed log seals metadata, not results.** Entries record kind,
  name, ok, and small meta (step counts, error snippets) — not full
  action results, which can be large and may contain live estate data.
  Tamper-evidence over *what ran and whether it worked* is the design
  goal; result payloads stay in the run records returned to callers.
- **No log rotation.** The log is append-only by design (rotation
  would break the chain); growth is one short line per execution. A
  future chain-anchored checkpoint (seal a segment, start a new file
  whose genesis is the segment's final seal) is the compatible path.
- **Nothing pushed.** Per instructions: no GitHub push, no
  `.github/workflows` changes, $0 spent, nothing deleted.

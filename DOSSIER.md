# omnitool — Dossier

*History, specification, metrics, tests and limits. Companion to `TECHNICAL-DISCLOSURE.md` and `README.md`.*

## What it is

`fleet-exec`, a batch executor for language-model agents: one JSON call carries many operations (bash, read, write,
patch, sql, glob, grep), which are **repaired, validated, guarded and executed** — in parallel, sequentially or in
dependency order — with one structured result per operation. An MCP server exposes it to any MCP-capable agent. In
the stack it is the **hands**. It grew out of the author's fleet, where it is the kernel every agent uses. Stage
(essay 07): system → primitive.

## Creation history

| Date (2026) | Milestone |
|---|---|
| Sep, early | `fleet-exec` in use inside the fleet, invoked by agents as a hand-typed JSON string in a shell — no schema |
| 09-18 | Whole-batch structural validation added: every violation reported at once, before anything runs (0 side effects); `--validate-only`; registered as an MCP tool with a schema |
| 09-19 | `concurrency: auto`: read/write dependency layers (parallel within a layer, layers in order); a write-write gap in the analyser fixed; patch fallback |
| 09-22 | Every call was paying a 10–14 s tax: the intent-classifier server was alive but not answering. Restart cut a call from 13.7 s to 0.4 s; the lesson (no supervision in the intelligence layer) led to the honest-failure tier chain |
| 09-23 | Serialization crash fixed; Dispatch Doctor (parse-error shell repair) ratified; C++ preflight soak 500 requests, 100% |
| 09-25 | Production pass: truncating writes fixed, Doctor double-run removed, bash operations ~40x faster |
| 09-26 | Dogfood pass (fleet bugs #42–59): bash instead of sh, SQL rows and counters kept, per-op timeouts honoured, abort semantics fixed |
| 09-29 | Release prep: guard made deterministic after measuring that the embedding classifier labelled `mkfs`, a fork bomb and a force-push "safe"; in-word apostrophe repair fixed; default write scope narrowed; ScanCode + gitleaks clean |
| 10-01 | Published: github.com/SovRing-Labs/omnitool v0.1.0 (Software Heritage save succeeded) |

## Specification (summary)

* **Contract:** `{"operations": [...], "concurrency": "parallel"|"sequential"|"auto", ...}` → `{"ok", "n", "results",
  "_intent", "_guard"}`; a refused batch → `{"ok": false, "errs": [...]}` with nothing executed.
* **Pipeline:** preflight repair → optional advisory classifier → structural validation (all errors at once) →
  deterministic guard (a BLOCK refuses the whole batch unless the op carries an explicit human-approved override) →
  execute (dependency layers, auto-detach on budget) → parse-error-only shell repair → failure-class retries behind a
  circuit breaker.
* **Honest tiers:** a classifier tier that is unimplemented or failing must answer with an explicit error, never a
  verdict.
* **Defaults:** SQL read-only unless `"mode": "read_write"`; writes limited to the tool root and `/tmp`
  (`FLEET_EXEC_WRITE_SCOPES` widens).

## Metrics (2026-09-29, Intel i5-8365U)

| Property | Result |
|---|---|
| Guard self-test | 59 / 59 |
| Guard cost | 48 µs per op check |
| Destructive verdicts (`mkfs`, fork bomb, force-push, `rm -rf /`, `DROP TABLE`) | all BLOCK; `ls -la`, `pytest` ok |
| Embedding classifier on the same inputs | mislabels 3 of 4 destructive commands "safe" — why the guard is deterministic |
| Side effects of a blocked batch | 0 |
| Preflight repair of malformed shapes | 8 / 10 |
| CLI wall time, one bash op | median 231 ms |
| 8 × `sleep 0.5` | parallel 0.97 s · auto 0.99 s · sequential 4.89 s |
| Auto-detach (`sleep 3`, budget 1 s) | returns `still_running` after 1.35 s |

## Tests

`make test` (re-run 2026-10-16): guard self-test 62/62; pytest 69 passed + 4 skipped (interceptor socket absent; that run
leaves out `tests/test_classifier.py`, which needs sentence-transformers); node:test 5/5 (11/11 with the MCP SDK installed). Earlier record: guard 59/59,
pytest 40 passed + 2 skipped.

## Limits (measured, not fixed in 0.1.0)

* Preflight: an `operations` value that is a JSON *string* of an array is split into one-character bash ops; a
  top-level Python-dict string is rejected.
* The C++ interceptor's inference is not implemented; it answers with an explicit error by design.
* The advisory classifier does not discriminate destructive commands; it is advisory only.

## Lineage and credits

Model Context Protocol (Anthropic and contributors); SQLite; the GNU `patch` program; sentence-embedding models for
the advisory tier (all-MiniLM-L6-v2 believed, excluded from the release). Full list: `PROVENANCE.md`. Updates:
weekly, recorded here with dates.

# OMNITOOL — Technical Disclosure

**Title:** A repair-validate-guard-execute pipeline for batched JSON tool calls from language-model agents: payload
repair for malformed tool calls, all-errors-at-once structural validation, a deterministic whole-batch safety guard with
explicit per-operation human override, dependency-layered parallel execution with budget-bounded auto-detach, a
parse-error-only shell repair pass, failure-class retries behind a circuit breaker, and a classifier tier chain in which
an unimplemented or failing tier must answer with an explicit error rather than a verdict.
**First public disclosure:** the date of the first public commit of this file. **Licence of the code:** PolyForm Small
Business 1.0.0 or PolyForm Noncommercial 1.0.0, user's choice (`LICENSING.md`). **Intent:** this document is published so
that everything it describes is prior art. The authors claim no patent on it and ask that none be granted to anyone on
what is disclosed here.
**Authors:** see `LICENSE.md` Required Notice. Cited prior work: `PROVENANCE.md`.

This is an enabling description: a practitioner can rebuild the system from this text alone. Every number in section 10
was measured on 2026-09-29 on a 2019 laptop CPU (Intel i5-8365U, 4 cores / 8 threads, no GPU) with the code in this
repository.

---

## 1. Summary

A language-model agent does its work through one tool whose argument is a JSON **batch** of operations. Between the
model and the operating system sits a fixed pipeline:

```
raw tool call → (1) preflight repair → (2) advisory classification (optional, never decides)
  → (3) structural validation (every violation reported at once; nothing runs on any error)
  → (4) deterministic guard (any BLOCK rule on any op refuses the whole batch; nothing runs)
  → (5) execution (parallel | sequential | dependency-layered) with per-op budget, repair, retry, breaker
  → (6) one structured result per op + batch verdict + guard warnings + safety provenance
```

The design goal is that small, local, error-prone tool-calling models can operate a real machine: malformed calls are
repaired when the repair is unambiguous, rejected with machine-readable corrections when not, and a dangerous batch is
refused **before any of its operations run**, so a batch never half-executes around a refusal.

## 2. The batch contract

* Request: `{"operations": [op, …], "concurrency": "parallel"|"sequential"|"auto", "timeout_s": n, "stop_on_error": b,
  "detach": b, "log_path": p}`. Each op has a `type` and type-specific fields:
  `bash{command, timeout_s|timeout_ms, detach, on_timeout, log_path}`, `read{path, cache_hash}`, `write{path, content, format}`,
  `patch{diff, target}`, `sql{sql|query|queries, database, mode, params}`, `glob{pattern, path, limit≤500}`,
  `grep{pattern, path, glob, ignore_case, limit≤300}`, `fix_json{path}`; any op may carry `on_failure: abort|continue`
  and `allow_destructive: true`.
* Response: `{"ok": all-ops-ok, "n", "duration_ms", "results": [per-op {ok, …, step, type}], "_guard": [warnings],
  "_intent": {intent, safety, safety_source}}`. A refused batch: `{"ok": false, "errs": ["op[i] …", …]}` and nothing ran.
* Successful op results are **compacted** (only actionable fields kept); failed ops keep full stdout/stderr/exit code,
  because the caller needs them to self-correct. Per-op internal keys (breaker hash, UI capsule) can be stripped by the
  transport layer.
* The same contract is exposed three ways: a CLI (argument, file or stdin), an MCP tool with a typed JSON Schema, and a
  `--validate-only` mode that runs stages 1–3 and returns the verdict without executing.

## 3. Preflight repair of malformed tool calls

Before validation the payload is normalised into `{"operations": [...]}`:
a naked op object is wrapped; a naked array is wrapped; a bare string is treated as one `bash` command; a JSON-encoded op
string is decoded; alias keys (`tasks`, `ops`, `commands`) are mapped to `operations`; per-op aliases (`tool`/`op` → `type`, `cmd` → `command`,
`query` → `sql`) are mapped to canonical names; the result carries a default `concurrency`. Repair never invents a
required field's content (a `write` without `content` is left missing so validation rejects it). The MCP transport
deliberately declares a **relaxed outer schema** (no required keys, `additionalProperties: true`) so a malformed call
reaches this repair and the self-correcting error messages instead of being rejected opaquely by the client or gateway;
an empty payload is answered with a literal corrected template and an instruction not to abandon the tool.

## 4. Structural validation — every violation at once

`validate_payload` walks the whole batch and returns **all** violations as short, machine-parseable strings
(`op[3]: missing 'content'`, `op[1]: unknown type 'git' — valid: … (did you mean 'patch' …?)`), so one correction round
fixes everything. Any violation means no operation runs. Type hints map likely confusions (git→patch, copy/move→bash,
query→sql).

## 5. Deterministic safety guard

Each op is checked against named rules; each rule has an id, a level and a reason:

* **BLOCK** (refuses the whole batch before any op runs): recursive `rm` of a protected root (`/`, `~`, `$HOME`, system
  directories, the user's primary work directories, `*` at top level), deleting or overwriting a live database file in the
  configured database directory, `sudo`, `mkfs`/`wipefs`, `dd`/redirect/`shred` onto a block device, fork bombs,
  shutdown/reboot/poweroff, `systemctl stop|disable|enable|mask|daemon-reload…`, `git push --force`, `git reset --hard`,
  `git clean -f`, `curl|wget … | sh`, SQL `DROP`, `TRUNCATE`, `DELETE` without `WHERE`.
* **WARN** (runs; reported in `_guard` and on stderr): recursive `rm` outside temp/scratch areas, `systemctl restart`,
  checkout-discard, `kill -9`/`killall`, `chmod 777`, network tools, `UPDATE` without `WHERE`.

Command parsing splits chains on `; & |`, strips wrappers (`sudo`, `env`, `nice`, …) before identifying the `rm`
program, and **does not scan heredoc bodies** (they are file content, not commands). Rules about *invoking* a program
(`sudo`, shutdown/reboot, `rm` targets) are anchored to command position, so the same words inside an `echo` string or
after a `#` comment do not fire; content rules (`mkfs`, fork-bomb pattern, device targets) match anywhere and so err on
the side of blocking. The override is **per
operation**: `"allow_destructive": true` on the one op a human approved; every other op in the batch is still checked. The
guard's verdict is the batch's reported safety (`safety_source: "guard"`); the classifier label is used only when no rule
fired and its confidence is above a floor (`safety_source: "classifier"`), else `"unrated"`. A built-in selftest lists
`must_block` and `must_pass` commands; a false positive is fixed by adding the command to `must_pass` and narrowing the rule.

## 6. Why the guard is deterministic: the embedding classifier does not discriminate

The optional advisory classifier embeds `"<type> <command|path|sql>"` with a small sentence-embedding model and compares it
to centroids of few-shot prototype lists for intents (read/write/bash/patch/sql/search) and safety (safe/network/
destructive). Measured today (section 10), dangerous and routine commands receive overlapping scores — `mkfs` and a fork
bomb score *higher* "safe" than "destructive", `git push --force` scores 0.028 destructive — so no threshold separates
them. The disclosed design therefore keeps the learned label **advisory only** and makes every blocking decision with
named, testable rules; a learned reflex model may later *add* signals but never silently replace a rule. A semantic cache
that would return an earlier result for a ≥0.98-similar payload is off by default, because similarity is not proof that a
bash/write/sql op is a no-op.

## 7. Classifier tier chain with honest failure (the interceptor idea)

Classification tries tiers in order and the first *valid* answer wins: (a) a native interceptor on a Unix socket
(C++ with an ONNX Runtime session held in locked memory, JSON over `AF_UNIX`, poll-guarded reads), (b) a warm HTTP daemon,
(c) an in-process cold import. The contract that makes the chain safe: **a tier that cannot really classify must reply
`{"error": …}`, and the caller treats an error key exactly like a dead tier and falls through.** In this release the
interceptor's `classify_text()` intentionally returns `not_implemented`; an earlier version returned a constant "safe"
verdict for every input (including `rm -rf /`), and because the first tier to answer wins, that fabricated verdict would
have masked the real classifier. The interceptor is opt-in (`FLEET_EXEC_BERT_SOCKET=1`), and a test asserts that it errors
rather than fabricates. The intended completion: WordPiece-tokenise, run the session, mean-pool, L2-normalise, cosine
against the same prototype centroids as tier (b)/(c), and add a discrimination test before it may answer.

## 8. Execution

* **Concurrency.** `parallel`: all ops at once (asyncio). `sequential`: in order. `auto`: build a dependency graph from
  each op's read set and write set (paths from `read`/`write`/`patch` targets, paths referenced in bash commands, SQL
  database targets), topologically sort it into **layers**, run each layer in parallel and the layers in order — a write
  followed by a read or write of the same path is serialised, independent ops keep full parallelism.
* **Budget-bounded auto-detach.** Every bash op without an explicit `detach`/`on_timeout` is launched in its own session
  with output to a log file and polled (5 ms doubling to 250 ms). If it finishes inside its budget the result is returned
  inline and the log is deleted; if not, it is **left running, never killed**, and the op returns
  `{ok: true, status: "still_running", pid, log_path, elapsed_s}`, so the caller never blocks past its own budget and a long
  build is not lost. `detach: true` is fire-and-forget; batch-level `detach` re-invokes the executor as a detached child
  for the whole batch.
* **Parse-error-only shell repair ("Dispatch Doctor").** A failed single-line bash op is re-run once with a repaired
  command **only if the shell rejected it at parse time** (syntax error / unexpected EOF / unmatched quote), i.e. nothing
  executed; repairs: close or strip an unbalanced trailing quote, re-quote via tokenise-and-join, or wrap in `bash -c`.
  The result is tagged `_doctor_fixed` / `_doctor_category` / original command. Restricting repair to parse failures
  prevents non-idempotent commands from running twice.
* **Retries and circuit breaker.** Failures are classified (timeout, database locked/busy, connection refused); only those
  classes retry, with exponential backoff plus up to 30% jitter (base 0.5 s for locks, 1 s otherwise, cap 15 s). Each op has
  a stable hash (type + command/path/database + SQL statement); consecutive failures per hash are counted in SQLite, and
  a tripped key aborts further retries of that op. Including the statement in the hash keeps one bad query from tripping
  the breaker for every query on the same database.
* **SQL.** Aliased or path databases; **read-only by default** (opened with `mode=ro`); a mutating statement in read-only
  mode is refused with the exact field to add (`"mode":"read_write"`); SELECTs are clamped to a row limit unless
  disabled; failed queries return available tables/schema hints.
* **Write scopes.** `write`/`patch`/`fix_json` targets are resolved through `realpath` and must lie under a configured
  directory list; the refusal names the allowed list.

## 9. Test and conformance
`make test` = guard selftest (59 cases) + pytest suite (preflight shapes, end-to-end CLI: repair, blocking with a
must-stay-absent marker file, per-op override, SQL defaults, glob/grep, quoting, circuit-breaker CLI, classifier
discrimination) + node:test suite (output condensing; MCP tool layer when the SDK is installed). Interceptor and
classifier tests SKIP with a stated reason when their optional components are absent.

## 10. Measured results (2026-09-29, this repository)

| Property | Result |
|---|---|
| Guard selftest | 59 / 59 PASS |
| Guard cost | 48 µs per op check (10,000 checks, Python, one core) |
| Guard verdicts: `mkfs.ext4 /dev/sda1`, fork bomb, `git push --force origin main`, `rm -rf /`, SQL `DROP TABLE` | all BLOCK (rules `mkfs`, `fork-bomb`, `git-force-push`, `rm-protected`, `sql-drop`); `ls -la`, `pytest` → ok |
| Embedding classifier, same inputs (safe / destructive score) | `mkfs` 0.155 / 0.136 → labelled **safe**; fork bomb 0.205 / 0.131 → **safe**; `git push --force` 0.139 / 0.028 → **safe**; `rm -rf /` 0.190 / 0.373 → destructive; `ls -la` 0.231 / 0.153 → safe; ≈16–22 ms per text |
| Blocked batch side effects | 0 (op[0] `touch marker` before a blocked op[1]: marker absent) |
| Preflight repair of malformed shapes | 8 / 10 (see limitations) |
| Parse-error repair | `echo 'hello`, `echo "hello` → re-run once, `_doctor_fixed` |
| CLI wall time, 1 bash op (process start + pipeline, classifier off) | median 231 ms (p10 222, p90 378; n = 20) |
| `--validate-only` wall time | median 226 ms (n = 20) |
| 8 × `sleep 0.5` | parallel 0.97 s · auto 0.99 s · sequential 4.89 s (median of 3) |
| `auto` write→read of one path | serialised; the read returns the written content |
| Auto-detach, `sleep 3` with `timeout_s: 1` | returns `still_running` after 1.35 s wall; process left running |
| Read-only SQL default | `CREATE TABLE` without `mode` refused with the `"mode":"read_write"` hint; with it, succeeds |
| Test suites | pytest 30 passed, 2 skipped (interceptor socket absent); guard 59/59; node:test 5/5 without SDK, 11/11 with SDK |

**Limitations measured today (not fixed in 0.1.0).** Preflight: an `operations` value that is a JSON *string* of an
array is split into one-character bash ops that pass validation; a top-level Python-dict string is rejected as a parse
error. Parse-error repair can change meaning: `echo it's fine` was repaired by appending a quote and printed `its fine`
(tagged `_doctor_fixed`, but the text changed).

## 11. Variations also disclosed

Any operation vocabulary (HTTP requests, browser actions, container or cloud API calls, database drivers other than SQLite,
file-system watches, GUI automation) under the same repair → validate → guard → execute contract; payloads in JSON, YAML,
MessagePack or protocol buffers; batches delivered by CLI, MCP, HTTP, gRPC, stdin, message queues or shared memory;
repair by rules, by a small model, or by a grammar-constrained decoder; validation reporting all errors, the first *k*
errors, or corrected payloads; guard rules expressed as regular expressions, parsed shell ASTs, eBPF/seccomp policies,
Datalog or policy languages (OPA/Rego), with any partition into block/warn/allow levels; whole-batch, per-layer or per-op
refusal; override by per-op flag, signed approval token, second model vote, or interactive human confirmation; learned
classifiers (embeddings, fine-tuned heads, ternary or 1-bit micromodels, VSA hypervector matching) used as advisory
signals, as additional block rules only after a measured discrimination test, or as tiers in a fall-through chain whose
members must return explicit errors when they cannot decide; tiers on Unix sockets, HTTP, shared memory or in-process;
dependency layering from declared, inferred or traced read/write sets; auto-detach with any polling schedule, log sink or
handle type (pid, job id, URL); repair passes limited to any class of pre-execution failure; retries by any failure
taxonomy and backoff curve; breakers keyed by any stable operation hash, per agent, per lane or per host; read-only
defaults and write scopes enforced in the executor, the transport, the kernel (namespaces, Landlock, AppArmor) or a
container; execution on any operating system and hardware.

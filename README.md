# Tarsus 0.2.0 — safe hands for tool-calling agents

<!-- status-badges (REPO-ACCESS-C) -->
[![status: pre-alpha (under development)](https://img.shields.io/badge/status-pre--alpha%20%28under%20development%29-orange)](STATUS.md) **Status: pre-alpha, under development** — what works today, what is untested and what failed: [`STATUS.md`](STATUS.md)

[![defensive publication: DOI 10.5281/zenodo.23282013](https://img.shields.io/badge/defensive%20publication-DOI%2010.5281%2Fzenodo.23282013-blue)](https://doi.org/10.5281/zenodo.23282013) **Defensive publication:** [`TECHNICAL-DISCLOSURE.md`](TECHNICAL-DISCLOSURE.md) · DOI [10.5281/zenodo.23282013](https://doi.org/10.5281/zenodo.23282013)
<!-- /status-badges -->

Tarsus (formerly *omnitool*) is the "hands" of an agent: one call carries a JSON batch of operations (`bash`, `read`,
`write`, `patch`, `sql`, `glob`, `grep`, `fix_json`), which is repaired, validated, safety-checked and executed in
parallel, sequentially or in dependency order, and answered with one structured result per operation. It is built
for small, local, tool-calling models that routinely emit malformed payloads: it repairs what it can, refuses what
it cannot, never runs half a batch its guard rejected — and since 0.2.0 the kernel itself confines what a bash
command can write, and only a human can approve a blocked operation.

Licence: **PolyForm Small Business 1.0.0 or PolyForm Noncommercial 1.0.0**, your choice (`LICENSING.md`).
Source-available, not OSI open source. Published as prior art (`TECHNICAL-DISCLOSURE.md`); no patents.
Provenance and third-party components: `PROVENANCE.md`. **Linux only** in this release.

```
agent ─► MCP (mcp/src/tarsus-server.js) ─► bin/fleet-exec  (or bin/fleet-execd, the warm fork server)
           preflight repair ─► structural validation ─► deterministic guard ─┬─ block ─► needs a HUMAN approval
                                                                             └─ ok ───► execute
           bash ops run under Landlock: writes only beneath the write scopes ─► per-op results (cuts announced)
```

## Layout
| Path | What |
|---|---|
| `bin/fleet-exec` | the executor (CLI: payload as argument, `--file`, or stdin `-`) |
| `bin/fleet-execd` | resident fork server: loads once, forks per request (~35 ms per tool call instead of ~200 ms) |
| `bin/tarsus_sandbox.py` | Landlock launcher every bash op runs under (writes confined to the write scopes) |
| `bin/tarsus` | setup + checks: `tarsus doctor`, `tarsus init`, `tarsus approve` |
| `bin/tarsus-approve` | the human approval channel in a terminal (`tarsus-approve` lists, `tarsus-approve <fp>` approves one run) |
| `bin/omnitool_guard.py` | deterministic safety rules (BLOCK / WARN) + one-shot approvals; `selftest` = 62 cases |
| `bin/omnitool_preflight.py` | repairs malformed agent payloads into `{"operations": [...]}` |
| `bin/fleet_sql.py` | SQLite batch engine behind `sql` ops (read-only by default, LIMIT clamping, schema hints) |
| `bin/fleet_patch.py` | unified-diff applier behind `patch` ops (`patch`, then `git apply --recount`) |
| `bin/fleet_dependency_graph.py` | read/write dependency layers for `"concurrency": "auto"` |
| `bin/fleet_semantic_validate.py`, `bin/fleet_retry_engine.py`, `bin/omnibreak` | conflict warnings, retry policy, circuit breaker |
| `bin/omnitool_intent_classifier.py`, `bin/omnitool_intent_daemon.py` | optional advisory intent label (does **not** decide safety) |
| `mcp/src/tarsus-server.js` | MCP server: `fleet_exec`, `fleet_exec_validate`, `read`, `read_many`, `write`, `edit`, `grep`, `glob`, `bash`, `sql` |
| `mcp/src/tarsus-core.js` | the tool set + call handler the server is built from |
| `interceptor/omnitool_bert_server.cpp` | experimental C++ interceptor (inference not implemented; answers with an explicit error) |
| `schemas/`, `systemd/`, `tests/`, `mcp/test/` | payload schema, optional user units, pytest + node:test suites |

## Quick start
```bash
bin/fleet-exec '{"operations": [
  {"type": "bash", "command": "uname -a"},
  {"type": "glob", "pattern": "*.py", "path": "bin"},
  {"type": "grep", "pattern": "def check_operation", "path": "bin"}
]}'
bin/fleet-exec --validate-only '{"operations": [{"type": "nope"}]}'   # structure check, nothing runs
make test
```
Output: `{"ok": bool, "n": int, "results": [...], "_intent": {...}, "_guard": [...]}`; a refused batch is
`{"ok": false, "errs": ["op[i] ..."]}` with nothing executed.

**Set up for an MCP client** (Claude Code, Claude Desktop, opencode, …):
```bash
cd mcp && npm install && cd ..
bin/tarsus doctor                                   # python, node 22+, rg, patch, git, Landlock, SDK
bin/tarsus init --project ~/code/myproj             # writes ~/.config/tarsus/{write-scopes.conf,tarsus.conf}, prints the entry
bin/tarsus init --project ~/code/myproj --client claude-code --write   # merges it into ~/code/myproj/.mcp.json
```
The MCP server starts the warm fork server (`bin/fleet-execd`) itself on first use; no service manager needed.

## Write scopes and the bash sandbox
`write`, `patch` and `fix_json` targets must lie beneath a **write scope**; since 0.2.0 the same scopes bind every
`bash` op and everything it spawns, enforced by the kernel (Linux Landlock, unprivileged, kernel ≥ 5.13). Reads,
exec and network are not restricted. A denied write fails with `Permission denied` and the result carries a
`sandbox_hint`.
- Scopes: `FLEET_EXEC_WRITE_SCOPES=dir1:dir2`, else `control-center/write-scopes.conf` (one directory per line),
  else the tool root and `/tmp`.
- `control-center/tarsus.conf`: `sandbox = enforce | best-effort | off` (default without the file: `best-effort`,
  which sandboxes when the kernel supports it) and `bash_extra_write = <dirs>` for tool caches (`~/.cache`,
  `~/.cargo`, …). Read on every call — no restart.
- Limits: Landlock grants existing directories only (add a new output directory before writing to it);
  `>/dev/stdout` / `>/dev/stderr` into a pipe is denied — use `>&1` / `>&2`.

## Approvals: only a human can unblock an operation
The guard **blocks** destructive operations (recursive delete of protected roots, `sudo`, `mkfs`, `dd` to a device,
force-push, `git reset --hard`, unit stop/disable, `DROP`/`TRUNCATE`/`DELETE`-all, pipe-to-shell, …). The refusal
names the rule and an **approval fingerprint**. The operation runs only after a person approves that exact operation:
- **Terminal:** `tarsus-approve` lists waiting operations; `tarsus-approve <fp>` shows it and asks `y/N`
  (needs an interactive terminal — an agent's pipe cannot approve).
- **In the MCP client:** clients that support MCP elicitation ask the user directly; on accept the server mints the
  approval and re-runs the call once.

An approval is valid for one run within 15 minutes and is stored in `TARSUS_APPROVAL_DIR` (default
`~/.config/tarsus/approvals`), outside every write scope and sandbox grant. The `allow_destructive` field an agent
can set is **not** an approval. WARN rules (recursive delete elsewhere, `kill -9`, `chmod 777`, network tools,
`UPDATE` without `WHERE`) run and are reported in `result["_guard"]`.

## Policy and audit
`tarsus.conf` is also the organisation's policy (read on every check — no restart):
```
rule.network = block                                   # re-level a built-in rule: block | warn | off
custom.no-prod-db = block :: any :: prod[-_]?db :: touches the production database
approval_ttl_s = 900                                   # how long a human approval stays valid (60 .. 86400 s)
audit = ~/.local/state/tarsus/audit.jsonl              # one record per operation; `off` disables
audit_detail = hash                                    # hash (no command text) | summary (first 200 chars)
```
`tarsus policy show` reports the effective policy (and any bad line); `tarsus policy check '<payload>'` shows what the
guard would decide without running anything. The agent-self-approval rule cannot be relaxed.

Each audit record is one JSON line in the **OpenTelemetry log data model** (`Timestamp`, `SeverityText`, `Body`,
`TraceId` per batch, `SpanId` per operation, `Attributes`, `Resource`): who (`enduser.id`, session, task, role), what
(type, fingerprint, input SHA-256, optional summary), the guard's decision (level, rules, who approved), the sandbox
mode and the outcome — including refused batches (`not_run`). Read it with `tarsus audit [--since 1h]
[--min-severity WARN] [--task T] [--stats]`; ship it with an OTel Collector:
```yaml
receivers:
  filelog/tarsus:
    include: [ ~/.local/state/tarsus/audit.jsonl ]
    operators:
      - type: json_parser
        timestamp: { parse_from: attributes.Timestamp, layout_type: epoch, layout: ns }
        severity: { parse_from: attributes.SeverityText }
```

## Configuration (environment)
| Variable | Default | Effect |
|---|---|---|
| `FLEET_EXEC_WRITE_SCOPES` / `OMNITOOL_SCOPES_FILE` | `control-center/write-scopes.conf`, else tool root + `/tmp` | write scopes (colon list / file) |
| `FLEET_EXEC_WRITE_ANYWHERE` | unset | `1` disables write scopes and the sandbox |
| `TARSUS_CONF` | `control-center/tarsus.conf` | sandbox mode + `bash_extra_write` |
| `FLEET_EXEC_SANDBOX`, `FLEET_EXEC_BASH_EXTRA_WRITE` | from `tarsus.conf` | override the conf file |
| `TARSUS_AUDIT`, `TARSUS_AUDIT_DETAIL` | from `tarsus.conf` (default off) | audit stream path / detail level |
| `TARSUS_APPROVAL_DIR` | `~/.config/tarsus/approvals` | one-shot human approvals + pending list |
| `OMNITOOL_DB_DIR` | `<tool root>/control-center` | SQL alias directory and the guard's live-database rule |
| `OMNITOOL_SQL_WRITE_HOOK` | `bin/compile-telemetry` if present | executable run (detached) after a SQL batch that mutated data |
| `OMNIBREAK_DB` | `control-center/omnibreak.db`, else `bin/omnibreak.db` | circuit-breaker database |
| `FLEET_EXEC_CLASSIFIER` / `OMNITOOL_INTENT_URL` | `on` / `http://127.0.0.1:8498` | advisory classifier off switch / daemon address |
| `FLEET_EXEC_STDOUT_CAP` | `16000` | characters of stdout kept per bash op (a cut says so in the output) |
| `FLEET_EXEC_SOCK` | `/tmp/fleet-execd.sock` | the MCP server uses the warm fork server when this socket answers |
| `FLEET_EXEC_SQL_AUTOPROMOTE`, `FLEET_EXEC_SEMANTIC_CACHE`, `FLEET_EXEC_SAFETY_MIN`, `FLEET_EXEC_BERT_SOCKET`, `FLEET_EXEC_MARKS` | off / off / `0.6` / off / `1` | as in 0.1.0 |
| MCP: `OMNITOOL_ROOT`, `MCP_MAX_CHARS`, `MCP_SPILL_DIR`, `MCP_KEEP_META`, `TARSUS_MCP_USAGE_LOG` | | tool root, output condensing, spill directory, keep internal metadata, optional usage log |

## Build
**Core (no build step).** Python 3.10+ standard library; Linux with Landlock for the sandbox. External programs:
`bash`, `patch` (GNU patch), `git` (patch fallback), `rg` (ripgrep). Tests: `pip install pytest`.

**MCP server.** Node 22+. `cd mcp && npm install` (installs `@modelcontextprotocol/sdk`, MIT).

**Optional advisory classifier.** `pip install sentence-transformers numpy` (+ `fastapi uvicorn pydantic faiss-cpu`
for the daemon). Measured: it does **not** separate dangerous from routine commands (`TECHNICAL-DISCLOSURE.md` §6);
safety comes from the guard and the sandbox.

**Optional, experimental interceptor** (`interceptor/omnitool_bert_server.cpp`): ONNX Runtime 1.18.x + nlohmann/json
3.11.x placed under `third_party/`, then `make interceptor`. It runs no inference by design (see `PROVENANCE.md`).

## Known limits
* Linux only (Landlock, `/proc`, GNU tools). macOS planned.
* `>/dev/stdout` / `>/dev/stderr` into a pipe is denied inside the sandbox (pipes are not paths).
* The `schemas/omnitool_operations.json` enum is a historical superset; `ALLOWED_OP_TYPES` in `bin/fleet-exec` is
  authoritative.

## Verify a release
Every GitHub release carries `tarsus-<ver>.tar.gz`, a CycloneDX SBOM `tarsus-<ver>.cdx.json`, `SHA256SUMS`, and a
Sigstore bundle (`*.sigstore.json`) for each — signed keylessly by this repository's release workflow
(`.github/workflows/release.yml`), recorded in the public Sigstore transparency log. To check a download:
```bash
python -m pip install sigstore
python -m sigstore verify identity tarsus-0.2.0.tar.gz \
  --bundle tarsus-0.2.0.tar.gz.sigstore.json \
  --cert-identity https://github.com/SovRing-Labs/tarsus/.github/workflows/release.yml@refs/tags/v0.2.0 \
  --cert-oidc-issuer https://token.actions.githubusercontent.com
sha256sum -c SHA256SUMS
```
The SBOM lists every shipped component (the MCP server's npm tree with exact versions, licences and hashes), the
external programs Tarsus calls but does not bundle, and the optional classifier packages; regenerate it from a
checkout with `bin/tarsus-sbom`.

## Dossier
History, specification, metrics, tests and limits: [`DOSSIER.md`](DOSSIER.md). Prior-art disclosures:
[`TECHNICAL-DISCLOSURE.md`](TECHNICAL-DISCLOSURE.md) (Part 1, 0.1.0) and [`TECHNICAL-DISCLOSURE-PART2.md`](TECHNICAL-DISCLOSURE-PART2.md)
(kernel-confined agent shell, operator-only approvals, whole-file guarantee). Release notes: `CHANGELOG.md`.

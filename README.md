# omnitool 0.1.0 — a JSON batch executor for tool-calling agents

omnitool is the "hands" of an agent fleet: one call carries a JSON batch of operations
(`bash`, `read`, `write`, `patch`, `sql`, `glob`, `grep`, `fix_json`), which is repaired, validated,
safety-checked and executed in parallel, sequentially or in dependency order, and answered with one
structured result per operation. It is built for small, local, tool-calling models that routinely emit
malformed payloads: the executor repairs what it can, refuses what it cannot, and never runs half a
batch that its safety guard rejected.

Licence: **PolyForm Small Business 1.0.0 or PolyForm Noncommercial 1.0.0**, your choice (`LICENSING.md`).
Source-available, not OSI open source. Published as prior art (`TECHNICAL-DISCLOSURE.md`); no patents.
Provenance and third-party components: `PROVENANCE.md`.

```
agent ─► MCP fleet_exec (mcp/) ─► bin/fleet-exec
           preflight repair ─► advisory classifier (optional) ─► structural validation
           ─► deterministic guard (block = whole batch refused) ─► execute ─► per-op results
```

## Layout
| Path | What |
|---|---|
| `bin/fleet-exec` | the executor (CLI: payload as argument, `--file`, or stdin) |
| `bin/omnitool_preflight.py` | repairs malformed agent payloads into `{"operations": [...]}` |
| `bin/omnitool_guard.py` | deterministic safety rules (BLOCK / WARN); `selftest` = 59 cases |
| `bin/fleet_sql.py` | SQLite batch engine behind `sql` ops (read-only by default, LIMIT clamping, schema hints) |
| `bin/fleet_patch.py` | unified-diff applier behind `patch` ops (calls the system `patch` program) |
| `bin/fleet_dependency_graph.py` | read/write dependency layers for `"concurrency": "auto"` |
| `bin/fleet_semantic_validate.py` | pre-execution conflict warnings (write/write races, stale reads) |
| `bin/fleet_retry_engine.py` | retry policy (backoff + jitter per failure class) |
| `bin/omnibreak` | circuit-breaker CLI over the SQLite breaker table fleet-exec keeps |
| `bin/omnitool_intent_classifier.py`, `bin/omnitool_intent_daemon.py` | optional advisory intent/safety label (embedding prototypes); the daemon serves it on `127.0.0.1:8498` |
| `interceptor/omnitool_bert_server.cpp` | experimental C++ Unix-socket interceptor (inference not implemented; answers with an explicit error) |
| `mcp/` | MCP server exposing `fleet_exec` and `fleet_exec_validate` |
| `schemas/omnitool_operations.json` | JSON Schema of the payload (historical superset of op names) |
| `systemd/` | optional user units for the daemon and the interceptor |
| `tests/`, `mcp/test/` | pytest and node:test suites |

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

## Configuration (environment)
| Variable | Default | Effect |
|---|---|---|
| `FLEET_EXEC_WRITE_SCOPES` | tool root and `/tmp` | colon list of directories `write`/`patch`/`fix_json` may touch |
| `FLEET_EXEC_WRITE_ANYWHERE` | unset | `1` disables write scopes |
| `OMNITOOL_DB_DIR` | `<tool root>/control-center` | directory for SQL aliases (`dispatch` → `dispatch.db`, `lanes` → `lane-catalog.db`, …) and the guard's live-database rule |
| `OMNITOOL_SQL_WRITE_HOOK` | unset | executable run (detached) after a SQL batch that mutated data |
| `OMNIBREAK_DB` | `bin/omnibreak.db` | circuit-breaker database |
| `FLEET_EXEC_CLASSIFIER` | `on` | `off` skips the advisory classifier (the guard still runs) |
| `OMNITOOL_INTENT_URL` | `http://127.0.0.1:8498` | classifier daemon address |
| `FLEET_EXEC_BERT_SOCKET` | unset | `1` probes the experimental interceptor socket first |
| `FLEET_EXEC_SQL_AUTOPROMOTE` | unset | `1` lets a write statement promote a read-only SQL op |
| `FLEET_EXEC_SEMANTIC_CACHE` | unset | `1` lets a cached classifier result short-circuit execution (off: similarity is not proof) |
| `FLEET_EXEC_SAFETY_MIN` | `0.6` | confidence floor for printing the advisory `[INTENT-SAFETY]` warning |
| `FLEET_EXEC_STDOUT_CAP` | `16000` | characters of stdout kept per bash op |
| `FLEET_EXEC_MARKS` | `1` | `0` disables the optional stigmergy-marks hook (a no-op unless a `weaver/marks.py` module exists under the tool root) |
| MCP: `OMNITOOL_ROOT`, `MCP_MAX_CHARS`, `MCP_SPILL_DIR`, `MCP_KEEP_META`, `OMNITOOL_MCP_USAGE_LOG` | | tool root, output condensing, spill directory, keep internal per-op metadata, optional usage log |

## Build
**Core (no build step).** Python 3.10+ standard library. External programs: `bash`, `patch` (GNU patch,
for `patch` ops), `rg` (ripgrep, for `grep` ops). Tests: `pip install pytest`.

**Optional advisory classifier.** `pip install sentence-transformers numpy` (Apache-2.0 / BSD-3-Clause; pulls
PyTorch, BSD-3-Clause). The model `sentence-transformers/all-MiniLM-L6-v2` (Apache-2.0) is downloaded by
sentence-transformers on first use. Daemon: `pip install fastapi uvicorn pydantic faiss-cpu` (MIT / BSD-3-Clause /
MIT / MIT). Measured: the classifier does **not** separate dangerous from routine commands (see
`TECHNICAL-DISCLOSURE.md` §6); safety comes from the guard.

**MCP server.** Node 22+. `cd mcp && npm install` (installs `@modelcontextprotocol/sdk`, MIT). Register with
your client, e.g. `{"command": "node", "args": ["<checkout>/mcp/src/index.js"]}`.

**Optional, experimental interceptor** (`interceptor/omnitool_bert_server.cpp`). Not shipped here, fetch yourself:
| Component | Version used in development | Upstream | Licence | Place at |
|---|---|---|---|---|
| ONNX Runtime (C/C++ API, prebuilt `onnxruntime-linux-x64-1.18.x.tgz`) | 1.18.0 (`ORT_API_VERSION 18`) | https://github.com/microsoft/onnxruntime/releases | MIT | `third_party/onnxruntime/{include,lib}` |
| nlohmann/json (single header `json.hpp`) | 3.11.2 | https://github.com/nlohmann/json/releases | MIT | `third_party/nlohmann/include/nlohmann/json.hpp` |
| BERT-class ONNX model | — | export your own, e.g. `optimum-cli export onnx --model sentence-transformers/all-MiniLM-L6-v2 bert_onnx/` (model: Apache-2.0), then rename `model.onnx` → `bert_onnx/bert_interceptor.onnx` | model's own | `bert_onnx/bert_interceptor.onnx` |

Then `make interceptor` (override `ORT_DIR=`, `JSON_INC=` if placed elsewhere). The binary resolves its model at
`<tool root>/bert_onnx/bert_interceptor.onnx` or takes a path as its first argument. `classify_text()` has **no
inference**: it returns `{"error": "not_implemented…"}` by design, so fleet-exec falls through to the next tier
instead of trusting a fabricated verdict. The development tree's model file is **not** redistributed (its
origin is undocumented; see `PROVENANCE.md`).

## Known issues (measured 2026-09-29, kept as-is in this release)
* Preflight repairs 8 of 10 malformed shapes tried. A top-level Python-dict string (`"{'type': 'bash', …}"`)
  is rejected as a JSON parse error (safe). An `operations` value that is a **JSON string of an array** is split
  into one-character bash commands that pass validation (`--validate-only` reports ok) — do not send that shape;
  fix planned upstream.
* The `schemas/omnitool_operations.json` enum is a historical superset; `bin/fleet-exec`'s `ALLOWED_OP_TYPES`
  is authoritative.

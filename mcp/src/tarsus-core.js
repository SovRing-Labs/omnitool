// Tarsus core — the release MCP tool set (TARSUS-0 B1, 2026-10-06).
// ONE source for both servers: src/tarsus-server.js (the public release: these tools only) and src/index.js (the
// fleet's sovereign-kernel: these tools + fleet-only ones). No fleet paths here: everything is relative to ROOT,
// the tool root = first ancestor of this file holding bin/fleet-exec (the checkout), or $OMNITOOL_ROOT. Host policy
// (e.g. a write gate that demands grounding first) is injected via makeCoreTools({writeGate}).
import { execFile, spawn } from "node:child_process";
import { createConnection } from "node:net";
import { existsSync } from "node:fs";
import { readFile, writeFile, appendFile, stat } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { condense, condenseBatch } from "./truncate.js";
import { render, scrub } from "./lean.js";

export const execFileAsync = promisify(execFile);
export const ROOT = process.env.OMNITOOL_ROOT || (() => {
  for (let d = dirname(fileURLToPath(import.meta.url)); d !== dirname(d); d = dirname(d))
    if (existsSync(join(d, "bin", "fleet-exec"))) return d;
  return resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
})();
const INTENT_DAEMON_URL = process.env.OMNITOOL_INTENT_URL || "http://127.0.0.1:8498";

// Warm path (2026-10-03): IPC-1 built bin/fleet-execd (a resident fork server running the
// unchanged fleet-exec main) and talon used it, but this server kept spawning a fresh Python fleet-exec per call
// (measured: kernel bash median 775 ms vs 19.5 ms through the socket). fleetExec() sends the call to the socket and falls
// back to spawning when the daemon is down. Same argv, same validation/guard/audit; result shaped like execFileAsync
// (rejects on a non-zero code with .stdout/.stderr attached).
const FLEET_EXEC_SOCK = process.env.FLEET_EXEC_SOCK || "/tmp/fleet-execd.sock";
function fleetExecSock(argv, { cwd = ROOT, timeout = 60000, maxBuffer = 16 * 1024 * 1024, stdin } = {}) {
  return new Promise((resolve, reject) => {
    const chunks = []; let size = 0; let done = false;
    const sock = createConnection(FLEET_EXEC_SOCK);
    const finish = (fn, v) => { if (!done) { done = true; clearTimeout(timer); fn(v); } };
    const timer = setTimeout(() => { sock.destroy(); const e = new Error(`fleet-execd timeout after ${timeout} ms`); e.killed = true; finish(reject, e); }, timeout);
    sock.on("connect", () => sock.write(JSON.stringify(stdin == null ? { argv, cwd } : { argv, cwd, stdin }) + "\n"));
    sock.on("data", (d) => { size += d.length; if (size > maxBuffer) { sock.destroy(); finish(reject, new Error("fleet-execd: maxBuffer exceeded")); } else chunks.push(d); });
    sock.on("error", (e) => { e.socketUnavailable = !chunks.length; finish(reject, e); });
    sock.on("end", () => {
      let r;
      try { r = JSON.parse(Buffer.concat(chunks).toString("utf8")); } catch (e) { e.socketUnavailable = false; return finish(reject, e); }
      if (r.code === 0) return finish(resolve, { stdout: r.stdout, stderr: r.stderr });
      const e = new Error(`fleet-exec exited ${r.code}: ${(r.stderr || "").slice(0, 500)}`);
      e.code = r.code; e.stdout = r.stdout; e.stderr = r.stderr; finish(reject, e);
    });
  });
}
// TARSUS-0 B3: no daemon -> start one (once per server process) so the NEXT call takes the ~35 ms warm path instead
// of a cold Python start (~0.4-1 s). Opt-in via TARSUS_AUTOSTART=1 — the release server sets it; the fleet leaves it
// unset because its fleet-execd is a systemd unit (a second instance would fight it for the socket).
let autostarted = false;
export function autostartDaemon() {
  if (autostarted || process.env.TARSUS_AUTOSTART !== "1" || !existsSync(`${ROOT}/bin/fleet-execd`)) return false;
  autostarted = true;
  try {
    const child = spawn("python3", [`${ROOT}/bin/fleet-execd`, "--sock", FLEET_EXEC_SOCK],
                        { cwd: ROOT, detached: true, stdio: "ignore" });
    child.on("error", () => {});
    child.unref();
    return child.pid;
  } catch { return false; }
}
export async function fleetExec(argv, opts = {}) {
  try {
    return await fleetExecSock(argv, opts);
  } catch (e) {
    if (!e.socketUnavailable) throw e;                      // daemon answered (or timed out): do not run the call twice
    autostartDaemon();
    const { stdin, ...rest } = opts;
    const p = execFileAsync(`${ROOT}/bin/fleet-exec`, argv, { cwd: ROOT, ...rest });
    if (stdin != null) p.child.stdin.end(stdin);
    return p;
  }
}


// Pre-flight engine: normalize payload using the canonical Python engine
async function runPreFlight(args) {
  try {
    const { stdout } = await execFileAsync(
      "python3",
      [`${ROOT}/bin/omnitool_preflight.py`, JSON.stringify(args)],
      { cwd: ROOT, timeout: 5000, maxBuffer: 1024 * 1024 }
    );
    return JSON.parse(stdout.trim());
  } catch (e) {
    // If pre-flight fails, fall through to local normalization (backward compat)
    console.error("[preflight] failed, using local normalization:", e.message);
    return null;
  }
}

// Intent classifier (BERT daemon): semantic intent, safety, anomaly detection, semantic cache
async function runIntentClassifier(args) {
  if (["off", "0", "false"].includes(String(process.env.FLEET_EXEC_CLASSIFIER || "on").toLowerCase())) return null;
  // Try daemon first (hotloaded, ~2ms)
  try {
    const response = await fetch(`${INTENT_DAEMON_URL}/classify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ payload: args }),
      signal: AbortSignal.timeout(3000)
    });
    if (response.ok) {
      return await response.json();
    }
  } catch (e) {
    // Fall through to local classifier
  }
  
  // TARSUS-0 B4: the cold in-process classifier (torch + MiniLM, seconds to ~50 s under load) runs only when asked —
  // the label is advisory; safety is the guard + sandbox. FLEET_EXEC_LOCAL_CLASSIFIER=1 restores it.
  if (process.env.FLEET_EXEC_LOCAL_CLASSIFIER !== "1") return null;
  // Fallback: local classifier (cold start ~2.5s)
  try {
    const { stdout } = await execFileAsync(
      "python3",
      [`${ROOT}/bin/omnitool_intent_classifier.py`, "classify", JSON.stringify(args)],
      { cwd: ROOT, timeout: 10000, maxBuffer: 1024 * 1024 }
    );
    return JSON.parse(stdout.trim());
  } catch (e) {
    console.error("[intent] classification failed:", e.message);
    return null;
  }
}


// TOOL-5 helpers: one fleet-exec call, parsed results; every flat verb answers {ok, data, error}.
export async function fx(operations, timeout = 120000) {
  let stdout;
  try {
    // Payload on stdin, not argv (bug #125): one argv string is capped at MAX_ARG_STRLEN = 128 KiB, so a write of a
    // >128 KB file died with spawn E2BIG on the no-daemon path.
    ({ stdout } = await fleetExec(["-"], { timeout, maxBuffer: 64 * 1024 * 1024, stdin: JSON.stringify({ operations }) }));
  } catch (e) {
    stdout = e.stdout || "";            // fleet-exec exits non-zero when ANY op fails; per-op results are still in stdout
    if (!stdout.includes("{")) throw e;
  }
  const j = JSON.parse(stdout.slice(stdout.indexOf("{")));
  // Bug #123: a payload refused at validation (e.g. write outside scopes) has {ok:false, errs:[...]} and no results;
  // returning [] made callers throw "Cannot read properties of undefined (reading 'ok')".
  if (!Array.isArray(j.results) || (operations.length && !j.results.length)) {
    const why = (j.errs && j.errs.join("; ")) || j.error || "fleet-exec returned no per-op results";
    throw new Error(why);
  }
  return j.results;
}
// TARSUS-0 A5: one ripgrep core for glob/grep and their legacy names (fleet_glob, fleet_grep). Measured before
// (mcp-tool-usage.jsonl): fleet_glob failed 56 % — it defaulted to path "/" and walked the whole filesystem into the
// 15 s timeout; fleet_grep failed 42 % — rg exit 1 ("no matches") was thrown as an error; flat glob/grep cut at 300
// lines without saying so. Zero matches is an answer; every cut is announced (A6).
export async function rgFiles(pattern, path, limit = 300) {
  let out = "";
  try { ({ stdout: out } = await execFileAsync("rg", ["--files", "-g", pattern, path || ROOT],
                                               { cwd: ROOT, timeout: 15000, maxBuffer: 8 * 1024 * 1024 })); }
  catch (e) { if (!(e.code === 1 || (e.code === 2 && e.stdout))) throw e; out = e.stdout || ""; }   // 2 + output = some paths unreadable
  const all = out.split("\n").filter(Boolean);
  return { files: all.slice(0, limit), total: all.length, truncated: all.length > limit };
}
export async function rgSearch({ pattern, path, glob, fixed = false, ignoreCase = false, invert = false, before = 0, after = 0,
                          maxDepth = null, perFile = null, limit = 300 }) {
  const args = ["--line-number", "--no-heading", "--color=never"];
  if (maxDepth != null) args.push("--max-depth", String(maxDepth));
  if (glob) args.push("-g", glob);
  if (before) args.push("-B", String(before));
  if (after) args.push("-A", String(after));
  if (invert) args.push("-v");
  if (fixed) args.push("-F");
  if (ignoreCase) args.push("-i");
  if (perFile) args.push("-m", String(perFile));
  args.push("-e", pattern, path || ROOT);
  let out = "";
  try { ({ stdout: out } = await execFileAsync("rg", args, { cwd: ROOT, timeout: 20000, maxBuffer: 8 * 1024 * 1024 })); }
  catch (e) { if (!(e.code === 1 || (e.code === 2 && e.stdout))) throw e; out = e.stdout || ""; }   // 2 + output = some paths unreadable
  const all = out.split("\n").filter((l) => l !== "");
  return { lines: all.slice(0, limit), total: all.length, truncated: all.length > limit };
}
export const cutNote = (r, n, what) => r.truncated ? `… [${what}: first ${n} of ${r.total} — narrow the pattern or path]` : null;

// Bug #130: read-modify-write must start from the WHOLE file. The default fleet-exec read is a 128 KB view; an edit on a
// 150 KB layout.rs wrote the view back (131,081 B, cut mid-statement) and reported {replaced:1}. Refuse unless the
// text read is provably every byte on disk.
export async function readWhole(path) {
  const r = (await fx([{ type: "read", path, full: true }]))[0];
  if (!r.ok) throw new Error(r.error || "read failed");
  const text = String(r.content ?? "");
  const size = (await stat(path)).size;
  const got = Buffer.byteLength(text, "utf8");
  if (r.truncated) throw new Error(`refusing: read of ${path} is truncated (${got} of ${size} bytes) — nothing written`);
  if (r.lossless === false) throw new Error(`refusing: ${path} is not valid UTF-8 — a text write-back would corrupt it; nothing written`);
  if (got !== size) throw new Error(`refusing: read ${got} bytes but ${path} is ${size} bytes on disk — nothing written`);
  return text;
}
export async function flatWrap(fn) {
  try { return { stdout: JSON.stringify({ ok: true, data: await fn(), error: null }) }; }
  catch (e) { return { stdout: JSON.stringify({ ok: false, data: e.data ?? null, error: String(e.message ?? e) }) }; }
}


// ── the tool set ───────────────────────────────────────────────────────────────────────────────────────────────
// TARSUS-0 B5: MCP tool annotations (spec 2025-06-18) so clients can auto-approve read-only tools and flag the rest.
// Hints, not guarantees — the guard, write scopes and the bash sandbox are the enforcement.
const RO = { readOnlyHint: true, openWorldHint: false };
export const CORE_ANNOTATIONS = {
  read: { title: "Read file", ...RO }, read_many: { title: "Read files", ...RO }, grep: { title: "Search contents", ...RO },
  glob: { title: "Find files", ...RO }, fleet_exec_validate: { title: "Validate a batch (nothing runs)", ...RO },
  write: { title: "Write file", readOnlyHint: false, destructiveHint: true, idempotentHint: true, openWorldHint: false },
  edit: { title: "Edit file (exact replace)", readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: false },
  bash: { title: "Run shell command (sandboxed)", readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: true },
  sql: { title: "Run SQL (read-only unless mode=read_write)", readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: false },
  fleet_exec: { title: "Run an operation batch", readOnlyHint: false, destructiveHint: true, idempotentHint: false, openWorldHint: true },
};
export function makeCoreTools({ writeGate = async () => {} } = {}) {
  const tools = makeCoreToolDefs(writeGate);
  for (const [name, a] of Object.entries(CORE_ANNOTATIONS)) if (tools[name]) tools[name].annotations = a;
  return tools;
}
function makeCoreToolDefs(writeGate) {
  return {
  fleet_exec: {
    description:
      "Runs bin/fleet-exec: batched bash/read/write/patch/sql operations in one " +
      "call (parallel by default). Every operation is validated structurally " +
      "before anything executes — a malformed batch is rejected whole, 0 side " +
      "effects, with every violation listed at once.",
    inputSchema: {
      // T_GAP-2 (Analysis.txt, 2026-09-20): Relax outer schema so malformed/empty
      // payloads pass the transport gate and hit server-side coercion. Previously,
      // `required: ["operations"]` killed {} at the transport layer — server
      // defensive logic never ran, model got opaque gateway error, panicked into
      // retry-loops. Now: transport accepts everything → server returns actionable
      // self-correct template with do_not_switch_tools directive.
      type: "object",
      properties: {
        operations: {
          oneOf: [
            { type: "array", minItems: 1, items: {
              type: "object",
              properties: {
                type: { type: "string", enum: ["bash","read","write","patch","sql","db_exec","omni_sql","glob","grep","fix_json"], description: "Operation type — bash, read, write, patch, sql, glob (find files), grep (search contents)." },
                pattern: { type: "string", description: "type=glob: glob like '**/*.py'; type=grep: regex." },
                glob: { type: "string", description: "type=grep only: restrict to files matching this glob." },
                allow_destructive: { type: "boolean", description: "Ignored as an approval (TARSUS-0 A4): a guard-blocked op runs only after a HUMAN approves its fingerprint (`tarsus-approve <fp>` in a terminal, or the client's approval prompt); then re-send the op unchanged." },
                command: { type: "string", description: "Shell command to execute (type=bash)." },
                path: { type: "string", description: "File path (type=read/write)." },
                content: { type: "string", description: "File content to write (type=write). Required. write/patch targets must lie under a write scope (control-center/write-scopes.conf or FLEET_EXEC_WRITE_SCOPES)." },
                diff: { type: "string", description: "Unified diff text (type=patch)." },
                target: { type: "string", description: "Explicit patch target path (type=patch, optional). Defaults to the path implied by the diff." },
                database: { type: "string", description: "DB alias or path (type=sql). Optional: defaults to \"dispatch\"." },
                mode: { type: "string", enum: ["read_only","read_write"], description: "SQL execution mode (default: read_only). Any INSERT/UPDATE/DELETE/DDL needs mode:\"read_write\" — there is no auto-promotion." },
                format: { type: "boolean", description: "type=write only: run the language formatter after writing (default false: bytes written exactly as given)." },
                timeout_ms: { type: "number", description: "Per-operation timeout override in ms. Overrides payload-level timeout_s for this specific op." },
                detach: { type: "boolean", description: "type=bash only: fire-and-forget. Spawns via a detached process and returns immediately." },
                log_path: { type: "string", description: "type=bash + detach/on_timeout=detach: where to redirect output (default: auto-named /tmp path)." },
                on_timeout: { type: "string", enum: ["detach"], description: "type=bash only: if the command is still running at timeout_s, dont kill it — leave it running and return status:still_running instead." },
                on_failure: { type: "string", enum: ["abort","continue"], description: "Per-operation failure policy. 'abort': a failure halts the remaining ops. 'continue' or unset: later ops still run (unset follows top-level stop_on_error, default false). A failed op always returns its own stdout/stderr/exit_code." },
              },
              required: ["type"],
              additionalProperties: true,
            } },
            { type: "object" },
            { type: "null" },
          ],
          description: "Array of operation objects, or single operation object.",
        },
        tasks: {
          type: "array",
          items: { type: "object" },
          description: "Alias for operations (common model hallucination).",
        },
        ops: {
          type: "array",
          items: { type: "object" },
          description: "Alias for operations.",
        },
        commands: {
          type: "array",
          items: { type: "object" },
          description: "Alias for operations.",
        },
        concurrency: {
          type: "string",
          enum: ["parallel", "sequential", "auto"],
          description:
            "'parallel': all ops at once, no dependency checking. 'sequential': one at a time. " +
            "'auto': computes real read/write dependency layers (fleet-dependency-graph.py) and " +
            "runs each layer in parallel, layers themselves sequential -- ops with no dependency " +
            "get full parallelism; a write followed by a read/write of the same path is correctly " +
            "serialized instead of racing. Prefer 'auto' over guessing parallel-vs-sequential by hand.",
        },
        stop_on_error: { type: "boolean", description: "Sequential mode only: halt on first failure" },
        detach: {
          type: "boolean",
          description:
            "Whole-batch background: run the ENTIRE operations list (in the " +
            "given concurrency mode — sequential for a dependent chain, " +
            "parallel for independent work) as one detached background job " +
            "and return {pid, log_path} immediately, without waiting on any of " +
            "it. Read log_path later (a bash op: cat/tail) to see progress or " +
            "the final combined JSON result.",
        },
        log_path: { type: "string", description: "With top-level detach: where to redirect the whole batch's combined output (default: an auto-named /tmp path)" },
        timeout_ms: {
          type: "number",
          description:
            "Overrides the outer transport timeout for this whole call (default 60000ms). " +
            "Raise this for a batch of slow foreground operations; clamped to [1000, 590000]. " +
            "For anything that might run longer than ~10 minutes, use a bash op with detach:true instead.",
        },
      },
      // Remove 'required' — transport gate must never kill the request before
      // server-side coercion and self-correct diagnostics run.
      additionalProperties: true,
    },
    run: async (args) => {
      // Run pre-flight engine for canonical normalization (handles malformed JSON,
      // naked objects, alias extraction, quote fixing, etc.)
      const preflight = await runPreFlight(args);
      if (preflight) {
        args = preflight;
      }

      // Intent classifier (BERT): semantic intent, safety, anomaly detection
      const intent = await runIntentClassifier(args);
      if (intent) {
        args._intent = intent;
        // Log warnings for anomalies and high-risk operations
        // D13: the :8498 daemon returns is_anomaly without anomaly_score (empty ops) — .toFixed on
        // undefined threw and replaced the self-correct reply with "Cannot read properties of undefined".
        if (intent.is_anomaly) {
          console.error(`[INTENT-ANOMALY] anomaly_score=${Number(intent.anomaly_score || 0).toFixed(3)} intent=${intent.intent}`);
        }
        if (intent.safety === "destructive" || intent.safety === "network") {
          console.error(`[INTENT-SAFETY] safety=${intent.safety} confidence=${Number(intent.safety_confidence || 0).toFixed(3)}`);
        }
      }

      // T_GAP-2 (Analysis.txt): Extract operations from primary key or common
      // model hallucinated aliases. Normalize to array BEFORE validation so
      // models get helpful errors instead of unhelpful API rejections.
      let ops = args.operations || args.tasks || args.ops || args.commands;

      // Handle completely empty payloads ({}) or missing keys — short-circuit
      // with an actionable self-correct response instead of letting fleet-exec
      // receive {} and crash with cryptic TypeError.
      if (!ops) {
        return {
          content: [{
            type: "text",
            text: `ERROR fleet_exec: missing 'operations' array. You submitted an empty payload ({}) or omitted the required wrapper.\n\nDo NOT switch to raw bash — re-issue fleet_exec immediately with this exact structure:\n\n{\n  "operations": [\n    { "type": "bash", "command": "echo hello" },\n    { "type": "read", "path": "/some/file.txt" }\n  ]\n}\n\nEach operation must have a 'type' field matching its required keys (read→path, bash→command, write→path+content, patch→diff, sql→database). Fix the payload and retry.`,
          }],
          isError: true,
        };
      }

      if (Array.isArray(ops) && ops.length === 0) {
        return {
          content: [{
            type: "text",
            text: `ERROR fleet_exec: 'operations' array is empty. You submitted {"operations": []}. Add at least one operation object and retry.\n\nCorrect format:\n{\n  "operations": [\n    { "type": "bash", "command": "ls -la" }\n  ]\n}`,
          }],
          isError: true,
        };
      }

      // Auto-normalize stringified JSON (models sometimes wrap arrays in quotes)
      if (typeof ops === "string") {
        try { ops = JSON.parse(ops); } catch(e) { /* will be caught below */ }
      }
      // Auto-wrap single operation object into array (models sometimes omit outer brackets)
      if (ops && typeof ops === "object" && !Array.isArray(ops)) {
        ops = [ops];
      }
      // Final check: must be a non-empty array at this point
      if (!Array.isArray(ops) || ops.length === 0) {
        return {
          content: [{
            type: "text",
            text: `ERROR fleet_exec: operations must be a JSON array of operation objects. Received: ${JSON.stringify(ops)}\n\nHINT: If this is a payload error — check operations is a real JSON array, each item has keys matching its type (path→read, command→bash). Fix the keys and retry.`,
          }],
          isError: true,
        };
      }

      // Restore normalized ops back to args for execFile
      args.operations = ops;
      for (let i = 0; i < ops.length; i++) {
        const op = ops[i];
        if (op.type === "read" && !op.path) {
          const errText = `ERROR (fleet_exec validation): Op ${i}: type=read missing required key 'path'. FIX: add {\"type\":\"read\",\"path\": \"<value>\"} to this operation.`;
          return { content: [{ type:"text", text: errText }], isError: true };
        }
        if (op.type === "write" && (!op.path || !op.content)) {
          const errText = `ERROR (fleet_exec validation): Op ${i}: type=write missing required key 'path' and/or 'content'.`;
          return { content: [{ type:"text", text: errText }], isError: true };
        }
        if (op.type === "bash" && !op.command) {
          const errText = `ERROR (fleet_exec validation): Op ${i}: type=bash missing required key 'command'. FIX: add {\"type\":\"bash\",\"command\": \"<value>\"} to this operation.`;
          return { content: [{ type:"text", text: errText }], isError: true };
        }
        if (op.type === "patch" && !op.diff) {
          const errText = `ERROR (fleet_exec validation): Op ${i}: type=patch missing required key 'diff'. FIX: add {\"type\":\"patch\",\"diff\": \"<value>\"} to this operation.`;
          return { content: [{ type:"text", text: errText }], isError: true };
        }
        if (op.type === "sql") {
          if (!op.database) {
            op.database = "dispatch";
          }
          if (op.query && !op.sql) {
            op.sql = op.query;
          }
        }
      }
      
      // Restore normalized ops back to args for execFile (redundant safety — 
      // already set at L212 after normalization, preserved for execFile compat)
      const isMutatingOp = (op) => {
        if (op.type === "write" || op.type === "patch") return true;
        if (op.type === "sql") {
          if (op.mode === "read_write") return true;
          const q = (op.sql || op.query || (Array.isArray(op.queries) ? op.queries.map(x => typeof x === 'string' ? x : x.sql).join(" ") : "")).trim().toUpperCase();
          return q.startsWith("INSERT") || q.startsWith("UPDATE") || q.startsWith("DELETE") || q.startsWith("DROP") || q.startsWith("ALTER") || q.startsWith("CREATE");
        }
        return false;
      };
      if (ops.some(isMutatingOp)) {
        await writeGate();
      }
      // 2026-09-26 ROOT CAUSE of "partial execution + bare HINT": args arrive with timeout_ms:null
      // (defaults filled in), Number(null) === 0 passed isFinite, and the clamp turned it into a
      // 1000 ms execFile timeout -- every call longer than 1 s was SIGTERM-killed after its side
      // effects ran, with its output lost. null/0/negative now mean "unset" (60 s default).
      const requested = args.timeout_ms == null ? NaN : Number(args.timeout_ms);
      const timeout = Number.isFinite(requested) && requested > 0 ? Math.min(Math.max(requested, 1000), 590000) : 60000;
      return fleetExec(["-"], { timeout, maxBuffer: 8 * 1024 * 1024, stdin: JSON.stringify(args) });   // stdin, not argv (E2BIG, bug #125)
    },
  },
    fleet_exec_validate: {
      description:
        "Structurally checks a fleet_exec payload WITHOUT running it — the " +
        "validate-then-execute pattern: plan first, validate deterministically, " +
        "execute only once the plan is clean.",
      inputSchema: {
        type: "object",
        properties: {
          operations: {
            oneOf: [
              { type: "array", items: { type: "object" } },
              { type: "object" },
              { type: "null" },
            ],
            description: "Array of operation objects, or single operation object.",
          },
          tasks: { type: "array", items: { type: "object" }, description: "Alias for operations." },
          ops: { type: "array", items: { type: "object" }, description: "Alias for operations." },
          commands: { type: "array", items: { type: "object" }, description: "Alias for operations." },
        },
        additionalProperties: true,
      },
      run: (args) =>
        fleetExec(["--validate-only", "-"], { timeout: 15000, stdin: JSON.stringify(args) }),
    },
    read: {
      description: "Read a file. Args: path (absolute), offset (line, default 0), max_lines (default 400). Returns {ok,data,error}.",
      inputSchema: { type: "object", properties: {
          path: { type: "string" }, offset: { type: "number" }, max_lines: { type: "number" } },
        required: ["path"], additionalProperties: false },
      run: async (a) => flatWrap(async () => {
        // full: total_lines + offset reach the real end (bug #130); past 32 MB keep the 128 KB view and say so.
        const big = await stat(a.path).then((s) => s.size > 32 * 1024 * 1024, () => false);
        const r = (await fx([{ type: "read", path: a.path, full: !big }]))[0];
        if (!r.ok) throw new Error(r.error || "read failed");
        if (r.truncated) r.content = String(r.content ?? "") + `\n… [first 128 KB of ${r.bytes} bytes — use bash sed/tail for the rest]`;
        const lines = String(r.content ?? "").split("\n");
        const off = Math.max(0, Number(a.offset) || 0), n = Math.max(1, Number(a.max_lines) || 400)  /* FLEET-TOOLS-COST-0 #4: 200 split one file over 3 calls */;
        return { path: a.path, total_lines: lines.length, offset: off, content: lines.slice(off, off + n).join("\n") };
      }),
    },
    read_many: {
      description: "Read several files in one call. Args: paths (array of absolute paths, max 20). Returns {ok,data:[{path,ok,content|error}]}.",
      inputSchema: { type: "object", properties: { paths: { type: "array", items: { type: "string" } } },
        required: ["paths"], additionalProperties: false },
      run: async (a) => flatWrap(async () => {
        const paths = (a.paths || []).slice(0, 20);
        const rs = await fx(paths.map((p) => ({ type: "read", path: p })));
        // A cut is always announced (bug #130 family): never hand back a silent prefix as if it were the file.
        return rs.map((r, i) => {
          if (!r.ok) return { path: paths[i], ok: false, error: r.error };
          const c = String(r.content ?? "");
          return c.length > 20000 || r.truncated
            ? { path: paths[i], ok: true, truncated: true, content: c.slice(0, 20000) + `\n… [cut at 20000 chars — page with read {path, offset, max_lines}]` }
            : { path: paths[i], ok: true, content: c };
        });
      }),
    },
    write: {
      description: "Create/overwrite a file (only beneath the write scopes: control-center/write-scopes.conf or FLEET_EXEC_WRITE_SCOPES). Args: path, content. Returns {ok,data,error}.",
      inputSchema: { type: "object", properties: { path: { type: "string" }, content: { type: "string" } },
        required: ["path", "content"], additionalProperties: false },
      run: async (a) => flatWrap(async () => {
        await writeGate();
        const r = (await fx([{ type: "write", path: a.path, content: a.content }]))[0];
        if (!r.ok) throw new Error(r.error || "write failed");
        return { path: a.path, bytes: Buffer.byteLength(a.content, "utf8") };
      }),
    },
    edit: {
      description: "Replace old_string with new_string in a file. old_string must occur EXACTLY ONCE (else error, nothing written). Args: path, old_string, new_string.",
      inputSchema: { type: "object", properties: {
          path: { type: "string" }, old_string: { type: "string" }, new_string: { type: "string" } },
        required: ["path", "old_string", "new_string"], additionalProperties: false },
      run: async (a) => flatWrap(async () => {
        await writeGate();
        const text = await readWhole(a.path);
        if (typeof a.old_string !== "string" || typeof a.new_string !== "string") throw new Error("edit needs string old_string and new_string (nothing written)");  // DL-omnitool-28: an omitted new_string used to be written as the text 'undefined'
        const n = a.old_string ? text.split(a.old_string).length - 1 : 0;
        if (n !== 1) throw new Error(`old_string found ${n} times — it must match exactly once (add surrounding lines)`);
        const w = (await fx([{ type: "write", path: a.path, content: text.replace(a.old_string, () => a.new_string) }]))[0];  // fn replacer: `$&`/`$'`/`$1` in new_string stay literal
        if (!w.ok) throw new Error(w.error || "write failed");
        return { path: a.path, replaced: 1 };
      }),
    },
    grep: {
      description: "Search file contents (ripgrep regex). Args: pattern, path (default: the tool root), glob (optional file filter like '*.py'). Returns {ok,data:'file:line:text' lines}.",
      inputSchema: { type: "object", properties: {
          pattern: { type: "string" }, path: { type: "string" }, glob: { type: "string" } },
        required: ["pattern"], additionalProperties: false },
      run: async (a) => flatWrap(async () => {
        const r = await rgSearch({ pattern: a.pattern, path: a.path, glob: a.glob, perFile: 50, limit: 300 });
        return [...r.lines, cutNote(r, 300, "lines")].filter(Boolean).join("\n");
      }),
    },
    glob: {
      description: "Find files by glob (ripgrep). Args: pattern (e.g. '**/*.py'), path (default: the tool root). Returns {ok,data:[paths]}.",
      inputSchema: { type: "object", properties: { pattern: { type: "string" }, path: { type: "string" } },
        required: ["pattern"], additionalProperties: false },
      run: async (a) => flatWrap(async () => {
        const r = await rgFiles(a.pattern, a.path, 300);
        return r.truncated ? [...r.files, cutNote(r, 300, "files")] : r.files;
      }),
    },
    bash: {
      description: "Run one shell command via the omnitool. Args: command, timeout_s (default 30), cwd (optional). Returns {ok (exit 0), data:{exit_code,stdout,stderr}}.",
      inputSchema: { type: "object", properties: {
          command: { type: "string" }, timeout_s: { type: "number" }, cwd: { type: "string" } },
        required: ["command"], additionalProperties: false },
      run: async (a) => flatWrap(async () => {
        // Bug #128: the transport timeout was a fixed 120 s, so timeout_s > 120 died as "fleet-execd timeout".
        const ts = Number(a.timeout_s) > 0 ? Number(a.timeout_s) : 30;
        const r = (await fx([{ type: "bash", command: a.command, cwd: a.cwd, timeout_s: ts, capture_stdout: true }],
                            Math.min(Math.max(ts * 1000 + 15000, 120000), 590000)))[0];
        const out = { exit_code: r.exit_code ?? r.returncode ?? (r.ok ? 0 : 1),
                      stdout: String(r.stdout ?? "").slice(-20000), stderr: String(r.stderr ?? "").slice(-4000) };
        if (!r.ok) { const e = new Error(r.error || `exit ${out.exit_code}`); e.data = out; throw e; }
        return out;
      }),
    },
    sql: {
      description: "Run SQL on a fleet SQLite db. Args: database (path), query, mode ('read_only' default | 'read_write'). Returns {ok,data:rows}.",
      inputSchema: { type: "object", properties: {
          database: { type: "string" }, query: { type: "string" }, mode: { type: "string", enum: ["read_only", "read_write"] } },
        required: ["database", "query"], additionalProperties: false },
      run: async (a) => flatWrap(async () => {
        if (a.mode === "read_write") await writeGate();
        const r = (await fx([{ type: "sql", database: a.database, query: a.query, mode: a.mode || "read_only" }]))[0];
        if (!r.ok) throw new Error(r.error || "sql failed");
        return r.rows ?? r.result ?? r.data ?? r;
      }),
    },
  };
}

// Review G4 (2026-10-06): flatWrap answers a failure as a normal reply ({"ok":false,...}) and legacy tools answer it
// as stderr with empty stdout, so the usage log said 0.0 % failures for read/edit/write/bash over 10,473 calls.
// Read the outcome from the reply itself: flat envelope ok, fleet-exec batch ok, legacy stderr-only.
export function replyOutcome(stdout, stderr) {
  const t = typeof stdout === "string" ? stdout.trim() : "";
  if (t.startsWith("{")) {
    try {
      const j = JSON.parse(t);
      if (j && typeof j === "object" && "ok" in j) {
        if (j.ok) {
          const ec = j.data && typeof j.data === "object" ? j.data.exit_code : undefined;   // flat bash: ok = exit 0
          return ec ? [false, `exit ${ec}`] : [true, null];
        }
        return [false, j.error || (Array.isArray(j.errs) && j.errs.join("; ")) || "ok:false"];
      }
    } catch { /* not JSON */ }
  }
  if (!t && stderr) return [false, String(stderr).split("\n")[0]];
  return [true, null];
}


// 2026-09-26 dogfood: fleet-exec per-op _capsule/_key_hash/_hash_prefix are internal
// (circuit-breaker key, UI capsule) — ~150 chars/op of noise that pushes real stdout past
// condense(). Stripped for MCP callers; MCP_KEEP_META=1 keeps them.
const META_KEYS = ["_capsule", "_key_hash", "_hash_prefix"];
export function stripMeta(text) {
  if (process.env.MCP_KEEP_META === "1" || typeof text !== "string" || !text.includes("_key_hash")) return text;
  try {
    const j = JSON.parse(text);
    const walk = (o) => { if (o && typeof o === "object") { for (const k of META_KEYS) delete o[k]; Object.values(o).forEach(walk); } };
    walk(j);
    // _intent: the BERT classifier's full per-op score dump (~700 chars/op) -> the 3 fields fleet-exec's CLI shows.
    if (j && j._intent && typeof j._intent === "object") {
      const { intent, safety, is_anomaly } = j._intent;
      j._intent = { intent, safety, is_anomaly };
    }
    return JSON.stringify(j);
  } catch { return text; }
}


// ── TARSUS-0 A4 channel 2 (2026-10-06; both channels): in-chat approval via MCP elicitation ───────────────────
// When the guard blocks an op it answers with "approval fingerprint: <fp>". If the client supports elicitation, the
// USER is asked in the client's own UI (not the model); on accept this server mints the one-shot token in
// TARSUS_APPROVAL_DIR and re-runs the call once. No elicitation support -> the reply already tells the human to
// run `tarsus-approve <fp>` in a terminal. The model never sees or sets the token.
const APPROVAL_DIR = (process.env.TARSUS_APPROVAL_DIR || "~/.config/tarsus/approvals").replace(/^~(?=\/|$)/, process.env.HOME || "");
export function approvalFp(text) {
  const m = String(text ?? "").match(/approval fingerprint: ([0-9a-f]{16})/);
  return m ? m[1] : null;
}
export async function elicitApproval(fp, srv) {
  if (!srv?.getClientCapabilities?.()?.elicitation) return false;
  let pend = {};
  try { pend = JSON.parse(await readFile(join(APPROVAL_DIR, "pending", `${fp}.json`), "utf8")); } catch { /* no pending row */ }
  let r;
  try {
    r = await srv.elicitInput({
      message: `The Tarsus guard blocked a destructive operation.\n\n${pend.op || "(op " + fp + ")"}\nrule: ${pend.why || "?"}\n\n` +
               `Approve this ONE run? (fingerprint ${fp}; one use, 15 min)`,
      requestedSchema: { type: "object", properties: { approve: { type: "boolean", title: "Approve this one run", default: false } },
                         required: ["approve"] },
    });
  } catch { return false; }                                   // client refused / not supported: terminal path stands
  if (r?.action !== "accept" || r?.content?.approve !== true) return false;
  const { mkdir, chmod } = await import("node:fs/promises");
  await mkdir(APPROVAL_DIR, { recursive: true, mode: 0o700 });
  const p = join(APPROVAL_DIR, `${fp}.json`);
  await writeFile(p, JSON.stringify({ fp, by: "elicitation", op: String(pend.op || "").slice(0, 500), ts: Date.now() / 1000 }), "utf8");
  await chmod(p, 0o600);
  return true;
}
export async function runWithApproval(tool, args, srv) {
  try {
    const out = await tool.run(args);
    const fp = approvalFp(JSON.stringify(out));
    return fp && (await elicitApproval(fp, srv)) ? await tool.run(args) : out;
  } catch (e) {
    const fp = approvalFp(`${e?.stdout ?? ""} ${e?.message ?? ""}`);
    if (fp && (await elicitApproval(fp, srv))) return await tool.run(args);
    throw e;
  }
}


// ── the call handler (usage log, approvals, rendering, condensing) ─────────────────────────────────────────────
export async function logToolUsage(usageLogPath, name, subset, ok, durationMs, err) {
  if (!usageLogPath) return;
  try {
    const row = { ts: Date.now(), tool: name, subset, ok, duration_ms: durationMs };
    if (!ok && err) row.err = String(err).slice(0, 160);
    await appendFile(usageLogPath, JSON.stringify(row) + "\n", "utf8");
  } catch { /* never let logging break a real tool call */ }
}
export function makeHandler({ getTool, server, subset = "release", usageLogPath = "", extraHint = null }) {
  const logToolUsage_ = (...a) => logToolUsage(usageLogPath, ...a);
return async function handleToolCall(request) {
  const tool = getTool(request.params.name);
  if (!tool) {
    throw new Error(`Unknown tool: ${request.params.name}`);
  }
  const t0 = Date.now();

  try {
    const out = await runWithApproval(tool, request.params.arguments ?? {}, server);   // A4: in-chat approval when blocked
    // D13 (2026-09-29, ported from release/omnitool mcp): a tool that builds its own self-correct reply
    // returns {content, isError}; destructuring {stdout} from it sent the agent an EMPTY text. Pass it through.
    if (out && out.content) {
      await logToolUsage_(request.params.name, subset, !out.isError, Date.now() - t0);
      return out;
    }
    const { stdout, stderr } = out;
    const [ok, why] = replyOutcome(stdout, stderr);   // TARSUS-0 A2: log what the caller got, not "did not throw"
    await logToolUsage_(request.params.name, subset, ok, Date.now() - t0, why);
    const body = tool.isFileRead ? stripMeta(stdout) : condenseBatch(stripMeta(stdout));   // bug #105: cut per op, not mid-batch
    const combined = [render(body), stderr && scrub(stderr)].filter(Boolean).join("\n");
    return {
      content: [{ type: "text", text: condense(combined, { isFileRead: !!tool.isFileRead }) }],
    };
  } catch (err) {
    await logToolUsage_(request.params.name, subset, false, Date.now() - t0, err?.message ?? err);
    const stdout = render(stripMeta(err.stdout ?? ""));
    // 2026-09-26 dogfood: execFile sets err.stderr = "" when the child wrote nothing to stderr, so
    // `??` threw away err.message (timeout / WRITE_BLOCKED / exit reason) and callers saw only the
    // generic HINT. Always carry the reason.
    const reason = [err.code != null ? `exit=${err.code}` : "", err.signal ? `signal=${err.signal}` : "",
                    err.killed ? "killed" : "",
                    // execFile's message echoes the whole argv (the full JSON payload) — keep only its verdict.
                    String(err.message ?? err).replace(/^Command failed: .*$/m, "Command failed")].filter(Boolean).join(" ");
    const stderr = [err.stderr, `REASON: ${reason}`].filter(Boolean).join("\n");
    // GAP-1: include self-correction hints for fleet_exec errors
    let hint = "";
    if (request.params.name === "fleet_exec") {
      // Only a payload error earns the payload hint; a nonzero/killed run carries its REASON instead.
      if (/payload|operations must be|missing required key|JSON/i.test(String(err.message ?? ""))) {
        hint = "\n\nHINT: If this is a payload error — check operations is a real JSON array, each item has keys matching its type (path→read, command→bash). Fix the keys and retry.";
      }
    } else if (extraHint) {
      hint = extraHint(request.params.name, request.params.arguments) || "";
    }
    return {
      content: [{ type: "text", text: condense([stdout, stderr, hint].filter(Boolean).join("\n")) }],
      isError: true,
    };
  }
};
}

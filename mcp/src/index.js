#!/usr/bin/env node
// omnitool MCP server (release 0.1.0).
// Exposes bin/fleet-exec to any MCP client as two tools: fleet_exec (run a batch) and
// fleet_exec_validate (check a batch without running it). Curated from the development
// server, which also carried fleet-specific tools; only the omnitool surface is released.
// See PROVENANCE.md ("MCP server") for exactly what was kept, changed and dropped.
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { execFile } from "node:child_process";
import { appendFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { condense } from "./truncate.js";

const execFileAsync = promisify(execFile);

// Tool root: OMNITOOL_ROOT, else the directory two levels above this file (<root>/mcp/src).
const OMNITOOL_ROOT = process.env.OMNITOOL_ROOT || resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const FLEET_EXEC = `${OMNITOOL_ROOT}/bin/fleet-exec`;
const PREFLIGHT_SCRIPT = `${OMNITOOL_ROOT}/bin/omnitool_preflight.py`;
const CLASSIFIER_SCRIPT = `${OMNITOOL_ROOT}/bin/omnitool_intent_classifier.py`;
const INTENT_DAEMON_URL = process.env.OMNITOOL_INTENT_URL || "http://127.0.0.1:8498";
const CLASSIFIER_OFF = ["off", "0", "false"].includes(String(process.env.FLEET_EXEC_CLASSIFIER || "on").toLowerCase());
// Optional usage log (one JSON line per call). Unset = no log.
const USAGE_LOG_PATH = process.env.OMNITOOL_MCP_USAGE_LOG || "";

// Pre-flight engine: normalize payload using the canonical Python engine
async function runPreFlight(args) {
  try {
    const { stdout } = await execFileAsync(
      "python3",
      [PREFLIGHT_SCRIPT, JSON.stringify(args)],
      { cwd: OMNITOOL_ROOT, timeout: 5000, maxBuffer: 1024 * 1024 }
    );
    return JSON.parse(stdout.trim());
  } catch (e) {
    // If pre-flight fails, fall through to local normalization (backward compat)
    console.error("[preflight] failed, using local normalization:", e.message);
    return null;
  }
}

// Intent classifier (advisory label only; the deterministic guard in fleet-exec decides safety)
async function runIntentClassifier(args) {
  if (CLASSIFIER_OFF) return null;
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

  // Fallback: local classifier (cold start ~2.5s)
  try {
    const { stdout } = await execFileAsync(
      "python3",
      [CLASSIFIER_SCRIPT, "classify", JSON.stringify(args)],
      { cwd: OMNITOOL_ROOT, timeout: 10000, maxBuffer: 1024 * 1024 }
    );
    return JSON.parse(stdout.trim());
  } catch (e) {
    console.error("[intent] classification failed:", e.message);
    return null;
  }
}

const TOOLS = {
  // fleet_exec: registering the batch executor as a typed MCP tool gives every MCP-capable
  // client provider-enforced structured tool-calling -- the model fills typed arguments, not a
  // raw JSON string it has to get exactly right. fleet-exec's own validate_payload() still
  // re-checks everything deterministically before any operation runs (belt and suspenders).
  fleet_exec: {
    description:
      "Runs bin/fleet-exec: batched bash/read/write/patch/sql/glob/grep operations in one " +
      "call (parallel by default). Every operation is validated structurally " +
      "before anything executes — a malformed batch is rejected whole, 0 side " +
      "effects, with every violation listed at once.",
    inputSchema: {
      // The outer schema is deliberately relaxed so malformed/empty payloads pass the
      // transport gate and reach server-side coercion, which answers with an actionable
      // self-correct template instead of an opaque gateway error.
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
                allow_destructive: { type: "boolean", description: "Only when a human approved it: lets an op the omnitool guard blocks (rm -rf of protected roots, sudo, mkfs, force-push, git reset --hard, systemctl stop/disable/enable, DROP/TRUNCATE/DELETE-all, pipe-to-shell) run. A blocked op refuses the whole batch." },
                command: { type: "string", description: "Shell command to execute (type=bash)." },
                path: { type: "string", description: "File path (type=read/write)." },
                content: { type: "string", description: "File content to write (type=write). Required. write/patch targets must be under a write scope (FLEET_EXEC_WRITE_SCOPES; default: the tool root and /tmp)." },
                diff: { type: "string", description: "Unified diff text (type=patch)." },
                target: { type: "string", description: "Explicit patch target path (type=patch, optional). Defaults to the path implied by the diff." },
                database: { type: "string", description: "DB alias or path (type=sql). Optional: defaults to \"dispatch\" (<OMNITOOL_DB_DIR>/dispatch.db)." },
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
        tasks: { type: "array", items: { type: "object" }, description: "Alias for operations (common model hallucination)." },
        ops: { type: "array", items: { type: "object" }, description: "Alias for operations." },
        commands: { type: "array", items: { type: "object" }, description: "Alias for operations." },
        concurrency: {
          type: "string",
          enum: ["parallel", "sequential", "auto"],
          description:
            "'parallel': all ops at once, no dependency checking. 'sequential': one at a time. " +
            "'auto': computes real read/write dependency layers and runs each layer in parallel, " +
            "layers themselves sequential -- a write followed by a read/write of the same path is " +
            "serialized instead of racing.",
        },
        stop_on_error: { type: "boolean", description: "Sequential mode only: halt on first failure" },
        detach: {
          type: "boolean",
          description:
            "Whole-batch background: run the ENTIRE operations list as one detached background job " +
            "and return {pid, log_path} immediately. Read log_path later to see the combined result.",
        },
        log_path: { type: "string", description: "With top-level detach: where to redirect the whole batch's combined output (default: an auto-named /tmp path)" },
        timeout_ms: {
          type: "number",
          description:
            "Overrides the outer transport timeout for this whole call (default 60000ms). " +
            "Clamped to [1000, 590000]. For anything longer, use a bash op with detach:true.",
        },
      },
      additionalProperties: true,
    },
    run: async (args) => {
      // Pre-flight engine: canonical normalization (malformed JSON, naked objects, aliases, quoting)
      const preflight = await runPreFlight(args);
      if (preflight) {
        args = preflight;
      }

      // Intent classifier: advisory label only
      const intent = await runIntentClassifier(args);
      if (intent) {
        args._intent = intent;
        if (intent.safety === "destructive" || intent.safety === "network") {
          console.error(`[INTENT-SAFETY] safety=${intent.safety} confidence=${Number(intent.safety_confidence || 0).toFixed(3)}`);
        }
      }

      // Extract operations from the primary key or common hallucinated aliases; normalize to an
      // array BEFORE validation so models get helpful errors instead of API rejections.
      let ops = args.operations || args.tasks || args.ops || args.commands;

      if (!ops) {
        return {
          content: [{
            type: "text",
            text: `ERROR fleet_exec: missing 'operations' array. You submitted an empty payload ({}) or omitted the required wrapper.\n\nDo NOT switch to raw bash — re-issue fleet_exec immediately with this exact structure:\n\n{\n  "operations": [\n    { "type": "bash", "command": "echo hello" },\n    { "type": "read", "path": "/some/file.txt" }\n  ]\n}\n\nEach operation must have a 'type' field matching its required keys (read→path, bash→command, write→path+content, patch→diff, sql→sql). Fix the payload and retry.`,
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
        try { ops = JSON.parse(ops); } catch (e) { /* will be caught below */ }
      }
      // Auto-wrap single operation object into array
      if (ops && typeof ops === "object" && !Array.isArray(ops)) {
        ops = [ops];
      }
      if (!Array.isArray(ops) || ops.length === 0) {
        return {
          content: [{
            type: "text",
            text: `ERROR fleet_exec: operations must be a JSON array of operation objects. Received: ${JSON.stringify(ops)}\n\nHINT: If this is a payload error — check operations is a real JSON array, each item has keys matching its type (path→read, command→bash). Fix the keys and retry.`,
          }],
          isError: true,
        };
      }

      args.operations = ops;
      for (let i = 0; i < ops.length; i++) {
        const op = ops[i];
        if (op.type === "read" && !op.path) {
          return { content: [{ type: "text", text: `ERROR (fleet_exec validation): Op ${i}: type=read missing required key 'path'. FIX: add {"type":"read","path": "<value>"} to this operation.` }], isError: true };
        }
        if (op.type === "write" && (!op.path || !op.content)) {
          return { content: [{ type: "text", text: `ERROR (fleet_exec validation): Op ${i}: type=write missing required key 'path' and/or 'content'.` }], isError: true };
        }
        if (op.type === "bash" && !op.command) {
          return { content: [{ type: "text", text: `ERROR (fleet_exec validation): Op ${i}: type=bash missing required key 'command'. FIX: add {"type":"bash","command": "<value>"} to this operation.` }], isError: true };
        }
        if (op.type === "patch" && !op.diff) {
          return { content: [{ type: "text", text: `ERROR (fleet_exec validation): Op ${i}: type=patch missing required key 'diff'. FIX: add {"type":"patch","diff": "<value>"} to this operation.` }], isError: true };
        }
        if (op.type === "sql") {
          if (!op.database) op.database = "dispatch";
          if (op.query && !op.sql) op.sql = op.query;
        }
      }

      // null/0/negative timeout_ms mean "unset" (60 s default): Number(null) === 0 once turned into
      // a 1000 ms timeout that SIGTERM-killed every call after its side effects ran.
      const requested = args.timeout_ms == null ? NaN : Number(args.timeout_ms);
      const timeout = Number.isFinite(requested) && requested > 0 ? Math.min(Math.max(requested, 1000), 590000) : 60000;
      return execFileAsync(FLEET_EXEC, [JSON.stringify(args)],
        { cwd: OMNITOOL_ROOT, timeout, maxBuffer: 8 * 1024 * 1024 });
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
      execFileAsync(FLEET_EXEC, ["--validate-only", JSON.stringify(args)],
        { cwd: OMNITOOL_ROOT, timeout: 15000 }),
  },
};

async function logToolUsage(name, ok, durationMs) {
  if (!USAGE_LOG_PATH) return;
  try {
    await appendFile(USAGE_LOG_PATH,
      JSON.stringify({ ts: Date.now(), tool: name, ok, duration_ms: durationMs }) + "\n", "utf8");
  } catch {
    // never let logging break a real tool call
  }
}

// Per-op _capsule/_key_hash/_hash_prefix are internal (circuit-breaker key, UI capsule) —
// ~150 chars/op of noise. Stripped for MCP callers; MCP_KEEP_META=1 keeps them.
const META_KEYS = ["_capsule", "_key_hash", "_hash_prefix"];
function stripMeta(text) {
  if (process.env.MCP_KEEP_META === "1" || typeof text !== "string" || !text.includes("_key_hash")) return text;
  try {
    const j = JSON.parse(text);
    const walk = (o) => { if (o && typeof o === "object") { for (const k of META_KEYS) delete o[k]; Object.values(o).forEach(walk); } };
    walk(j);
    if (j && j._intent && typeof j._intent === "object") {
      const { intent, safety, safety_source, is_anomaly } = j._intent;
      j._intent = { intent, safety, safety_source, is_anomaly };
    }
    return JSON.stringify(j);
  } catch { return text; }
}

const server = new Server(
  { name: "omnitool-mcp-server", version: "0.1.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: Object.entries(TOOLS).map(([name, tool]) => ({
    name,
    description: tool.description || "",
    inputSchema: tool.inputSchema || {},
  })),
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const tool = TOOLS[request.params.name];
  if (!tool) {
    throw new Error(`Unknown tool: ${request.params.name}`);
  }
  const t0 = Date.now();
  try {
    const out = await tool.run(request.params.arguments ?? {});
    if (out && out.content) return out;       // self-correct responses built by the tool itself
    const { stdout, stderr } = out;
    await logToolUsage(request.params.name, true, Date.now() - t0);
    const combined = [stripMeta(stdout), stderr].filter(Boolean).join("\n");
    return { content: [{ type: "text", text: condense(combined) }] };
  } catch (err) {
    await logToolUsage(request.params.name, false, Date.now() - t0);
    const stdout = stripMeta(err.stdout ?? "");
    // execFile sets err.stderr = "" when the child wrote nothing; always carry the reason.
    const reason = [err.code != null ? `exit=${err.code}` : "", err.signal ? `signal=${err.signal}` : "",
                    err.killed ? "killed" : "",
                    // execFile's message echoes the whole argv (the full JSON payload) — keep only its verdict.
                    String(err.message ?? err).replace(/^Command failed: .*$/m, "Command failed")].filter(Boolean).join(" ");
    const stderr = [err.stderr, `REASON: ${reason}`].filter(Boolean).join("\n");
    let hint = "";
    if (/payload|operations must be|missing required key|JSON/i.test(String(err.message ?? ""))) {
      hint = "\n\nHINT: If this is a payload error — check operations is a real JSON array, each item has keys matching its type (path→read, command→bash). Fix the keys and retry.";
    }
    return {
      content: [{ type: "text", text: condense([stdout, stderr, hint].filter(Boolean).join("\n")) }],
      isError: true,
    };
  }
});

// MCP_TEST_MODE=1 lets tests import this module without starting a stdio server.
if (process.env.MCP_TEST_MODE !== "1") {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

export { TOOLS, OMNITOOL_ROOT };

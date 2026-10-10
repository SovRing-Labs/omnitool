// LEAN (claude-seat 2026-10-02, J: "mcp kernel tools are using a lot of context — make the tool calls scrub themselves
// or lower context usage when attached to other providers/harnesses"). Measured before: one Claude seat session spent
// ~157k tokens on kernel results (264 bash calls, avg 1.4k chars) and every non-deferring harness pays ~4.9k tokens/turn
// for the 28 tool schemas.
//
// 1. render(): the flat tools reply {"ok":..,"data":{..},"error":..} as ONE JSON line — escaped newlines/quotes, an
//    envelope on every call, and condense() could not cut it by lines. Render it as plain text instead:
//      bash  -> stdout as-is, "[exit N]" only when non-zero, "[stderr]" only when non-empty
//      read  -> "[path · lines a–b of N]" + content
//      sql   -> TSV (header + rows)
//      other -> compact JSON
// 2. scrub(): strip ANSI escapes, collapse runs of identical lines ("… ×N"), trim trailing spaces.
// 3. Tool profile (listing only): MCP_TOOL_PROFILE=core|full, default auto — "full" for Claude Code (it defers MCP
//    schemas behind tool search), "core" for every other client (opencode, Gemini, Desktop chat, Talon engines):
//    the 9 base tools + fleet_exec + fleet_kb_search + board_query, with fleet_exec's long description replaced by
//    a short one. Excluded tools stay callable by name; only their schemas leave the context.
// Off switch: MCP_LEAN=0 (raw JSON replies, full listing).

export const LEAN = process.env.MCP_LEAN !== "0";

const ANSI = /\x1b\[[0-9;?]*[ -\/]*[@-~]/g;

export function scrub(text) {
  if (typeof text !== "string") return text;
  const lines = text.replace(ANSI, "").split("\n").map((l) => l.replace(/[ \t]+$/, ""));
  const out = [];
  for (let i = 0; i < lines.length; ) {
    let j = i;
    while (j + 1 < lines.length && lines[j + 1] === lines[i]) j++;
    const n = j - i + 1;
    if (n >= 4 && lines[i] !== "") { out.push(lines[i], `… ×${n - 1} more identical lines`); }
    else for (let k = i; k <= j; k++) out.push(lines[k]);
    i = j + 1;
  }
  return out.join("\n");
}

function tsv(rows) {
  if (!Array.isArray(rows) || !rows.length || typeof rows[0] !== "object") return null;
  const cols = Object.keys(rows[0]);
  const cell = (v) => (v === null || v === undefined ? "" : String(typeof v === "object" ? JSON.stringify(v) : v)
    .replace(/\t/g, " ").replace(/\n/g, "⏎"));
  return [cols.join("\t"), ...rows.map((r) => cols.map((c) => cell(r[c])).join("\t"))].join("\n");
}

function renderData(d) {
  if (d === null || d === undefined) return "";
  if (typeof d === "string") return d;
  if (typeof d !== "object") return String(d);
  // bash: {exit_code, stdout, stderr}
  if ("stdout" in d && ("exit_code" in d || "stderr" in d)) {
    const parts = [];
    if (d.exit_code) parts.push(`[exit ${d.exit_code}]`);
    if (d.stdout) parts.push(String(d.stdout).replace(/\n$/, ""));
    if (d.stderr) parts.push(`[stderr] ${String(d.stderr).replace(/\n$/, "")}`);
    return parts.join("\n") || "(no output)";
  }
  // read: {path, content, offset, total_lines}
  if ("content" in d && "path" in d && typeof d.content === "string") {
    const n = d.content ? d.content.split("\n").length : 0;
    const a = (d.offset || 0) + 1;
    const head = `[${d.path} · lines ${a}–${a + n - 1}${d.total_lines != null ? ` of ${d.total_lines}` : ""}]`;
    return `${head}\n${d.content}`;
  }
  // sql: {ok, results:[{rows:[...]}]} or rows directly
  if (Array.isArray(d.results) && d.results.every((r) => r && Array.isArray(r.rows))) {
    return d.results.map((r) => tsv(r.rows) ?? `(${r.count ?? r.n ?? 0} rows)`).join("\n---\n");
  }
  if (Array.isArray(d) && tsv(d)) return tsv(d);
  return JSON.stringify(d);
}

// Render a tool's stdout for the model. Non-envelope text passes through (scrubbed).
export function render(text) {
  if (!LEAN || typeof text !== "string") return text;
  const t = text.trim();
  if (t.startsWith("{") && t.includes('"ok"')) {
    try {
      const j = JSON.parse(t);
      if (j && typeof j === "object" && "ok" in j && ("data" in j || "error" in j)) {
        const body = renderData(j.data);
        const out = j.ok ? body : [`ERROR: ${j.error ?? "failed"}`, body].filter(Boolean).join("\n");
        return scrub(out);
      }
    } catch { /* not an envelope */ }
  }
  return scrub(text);
}

// ---- tool profile ---------------------------------------------------------------------------------------------
export const CORE_TOOLS = new Set(["read", "read_many", "write", "edit", "grep", "glob", "bash", "sql", "web_fetch",
  "fleet_exec", "fleet_kb_search", "board_query"]);

export const SHORT_DESCRIPTIONS = {
  fleet_exec: "Batch file/shell/sql ops in one call through bin/fleet-exec (write scopes, audit, field marks). " +
    "operations: [{type:'bash',command,timeout_s?} | {type:'read',path} | {type:'write',path,content} | " +
    "{type:'patch',path,old_string,new_string} | {type:'sql',database,query,mode?}]. mode: 'parallel'|'sequential'.",
};

export function profileFor(clientName) {
  const p = process.env.MCP_TOOL_PROFILE;
  if (p === "core" || p === "full") return p;
  if (!LEAN) return "full";
  return /claude-code/i.test(clientName || "") ? "full" : "core";
}

// Core profile: nested property descriptions capped at 90 chars (fleet_exec's input schema alone is ~5k chars).
export function trimSchema(schema, depth = 0) {
  if (!schema || typeof schema !== "object") return schema;
  if (Array.isArray(schema)) return schema.map((s) => trimSchema(s, depth + 1));
  const o = {};
  for (const [k, v] of Object.entries(schema)) {
    if (k === "description" && typeof v === "string" && depth > 0 && v.length > 90) o[k] = v.slice(0, 87) + "…";
    else if (k === "examples" || k === "$comment") continue;
    else o[k] = trimSchema(v, depth + 1);
  }
  return o;
}

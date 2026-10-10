// ┌─ CORE FILE · governed by control-center/core-changelog.jsonl ──────────
// │ ⚠  LOAD-BEARING. 8 logged change(s). Read the ledger before editing:
// │     bin/core-changelog show mcp/sovereign-mcp-server/src/truncate.js
// │ Last: 2026-10-06T06:56Z · jc · 3a2350183 · checkpoint: auto 2026-10-05 23:56 (37 files on t126-lifecycle-spine)
// └─────────────────────────────────────────────
// MCP_MAX_CHARS (2026-09-26 dogfood): 3000 suits small-model callers; a large-context seat
// raises it via the server env in .mcp.json. Invalid/unset -> 3000.
import { createHash } from "node:crypto";
import { mkdirSync, readdirSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";

// LEAN (2026-10-02): a dispatched worker (TALON_TASK_ID set — Claude heads inherit the seat's 12000 from .mcp.json)
// gets MCP_WORKER_MAX_CHARS (default 6000); the full text still spills to a pageable file.
const MAX_CHARS = (() => {
  const raw = process.env.TALON_TASK_ID ? (process.env.MCP_WORKER_MAX_CHARS ?? "6000") : process.env.MCP_MAX_CHARS;
  const n = Number(raw); return Number.isFinite(n) && n >= 1000 ? n : 3000;
})();
const HEAD_LINES = 30;
// Bug #45 (2026-09-26): elided output had no way back. The full text now spills to a file
// named in the marker; page it with fleet_file_read {path, offset, max_lines}.
const SPILL_DIR = process.env.MCP_SPILL_DIR || "/tmp/mcp-spill";
const SPILL_KEEP = 200;   // newest files kept; older ones pruned on write

function spill(text) {
  try {
    mkdirSync(SPILL_DIR, { recursive: true, mode: 0o700 });
    const name = `${Date.now()}-${createHash("sha1").update(text).digest("hex").slice(0, 10)}.txt`;
    const path = join(SPILL_DIR, name);
    writeFileSync(path, text, { mode: 0o600 });
    const files = readdirSync(SPILL_DIR).filter((f) => f.endsWith(".txt")).sort();
    for (const f of files.slice(0, Math.max(0, files.length - SPILL_KEEP))) {
      try { unlinkSync(join(SPILL_DIR, f)); } catch {}
    }
    return path;
  } catch {
    return null;   // best-effort: a spill failure must never break the tool reply
  }
}
const TAIL_LINES = 15;

// Bug #105 (KERNEL-DOGFOOD-1): a fleet_exec batch is ONE JSON line, so condense() cut its middle — a small sql
// result sitting between two big bash outputs vanished. Shrink per op instead: water-fill the char budget across
// the string fields of results[] (small ones stay whole, big ones share the rest), each cut field spilled in full.
const BATCH_FIELDS = ["stdout", "stderr", "content", "error"];
export function condenseBatch(text) {
  if (typeof text !== "string" || text.length <= MAX_CHARS) return text;
  let j;
  try { j = JSON.parse(text); } catch { return text; }
  if (!j || !Array.isArray(j.results)) return text;
  const fields = [];
  j.results.forEach((r, i) => {
    if (r && typeof r === "object") for (const k of BATCH_FIELDS)
      if (typeof r[k] === "string") fields.push({ r, k, i, n: JSON.stringify(r[k]).length });
  });
  const fixed = text.length - fields.reduce((s, f) => s + f.n, 0);
  let left = Math.floor((MAX_CHARS - fixed) * 0.9) - 160 * fields.length;   // room for the cut markers
  if (left <= 0) return text;
  const order = [...fields].sort((a, b) => a.n - b.n);
  let cap = Infinity;
  for (let x = 0; x < order.length; x++) {
    const share = Math.floor(left / (order.length - x));
    if (order[x].n <= share) { left -= order[x].n; continue; }
    cap = Math.max(share, 200); break;
  }
  if (cap === Infinity) return text;
  for (const f of fields) {
    if (f.n <= cap) continue;
    const s = f.r[f.k], half = Math.floor(cap / 2);
    const full = spill(s);
    f.r[f.k] = s.slice(0, half) + `\n… [op ${f.i} ${f.k}: ${s.length - cap} chars omitted` +
      (full ? ` full: ${full}` : "") + ` …]\n` + s.slice(-half);
  }
  return JSON.stringify(j);
}

// Head-and-tail truncation for command output. File reads bypass this entirely --
// the reading tool's OWN max_lines/max_bytes params (fleet_file_read defaults to
// 64KB) are the real, explicit bound the caller chose; re-clipping to ~3000 chars
// here on top of that silently broke the tool's own advertised 64KB limit (2026-09-18).
export function condense(text, { isFileRead = false } = {}) {
  if (typeof text !== "string") return text;

  if (isFileRead) {
    return text;
  }

  if (text.length <= MAX_CHARS) {
    return text;
  }

  const full = spill(text);
  const lines = text.split("\n");
  if (lines.length > HEAD_LINES + TAIL_LINES) {
    // Enough lines for line-based head/tail to actually shrink the output.
    const head = lines.slice(0, HEAD_LINES);
    const tail = lines.slice(-TAIL_LINES);
    const omitted = lines.length - head.length - tail.length;
    return [
      ...head,
      `\n… [${omitted} lines omitted, ${text.length} chars total]` +
        (full ? ` full: ${full} — fleet_file_read {path, offset: ${HEAD_LINES}, max_lines} to page` : "") + ` …\n`,
      ...tail,
    ].join("\n");
  }

  // Too few lines for line-based truncation to help (e.g. one giant
  // single-line blob: minified JSON, base64, a long URL). The old logic fell
  // through to head=tail=[the whole line], which duplicated it instead of
  // shrinking it -- output ended up LARGER than the input. Fall back to a
  // character-based head/tail split instead.
  const half = Math.floor(MAX_CHARS / 2);
  return text.slice(0, half) +
    `\n… [${text.length - MAX_CHARS} chars omitted]` + (full ? ` full: ${full}` : "") + ` …\n` +
    text.slice(-half);
}

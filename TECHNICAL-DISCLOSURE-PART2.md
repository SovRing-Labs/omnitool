---
title: TARSUS — Technical Disclosure, Part 2 (2026-10-06)
updated: 2026-10-06
weaver_hash: fe6d31dcf5cd7a9afeee7ff7061d5e38aff267f999c71e4220f6d24d9859acef
weaver_imported: 2026-10-09T22:22:08+00:00
---
# TARSUS — Technical Disclosure, Part 2 (2026-10-06)

**Title:** Kernel-confined command execution for language-model agents with operator-only, one-shot, operation-bound
approvals: (a) every shell operation an agent submits — and every process it spawns — runs under an unprivileged
kernel filesystem-write confinement whose grants equal the executor's own write scopes; (b) an operation refused by a
deterministic guard runs only after a human mints a single-use approval bound to a fingerprint of that exact
operation, through a channel the agent cannot drive; (c) read-modify-write tools refuse to write unless the bytes they
read are provably the whole file, and every truncated output announces itself.
**First public disclosure:** the date of the first public commit of this file. **Licence of the code:** PolyForm Small
Business 1.0.0 or PolyForm Noncommercial 1.0.0 (`LICENSING.md`). **Intent:** published so that everything described
here is prior art; the authors claim no patent and ask that none be granted to anyone on it. Part 1:
`TECHNICAL-DISCLOSURE.md`. This is an enabling description; numbers were measured 2026-10-06 on the development host
(Linux 7.0, Landlock ABI 8).

---

## 1. The gap Part 1 left
Part 1's guard and write scopes were enforced in the executor: `write`/`patch` targets were checked, but a `bash`
operation was a free string — `echo x > /var/tmp/y` wrote anywhere (measured). And a blocked operation ran whenever
the caller set `"allow_destructive": true`, a field the model itself controls.

## 2. Kernel-confined shell operations
1. The executor computes the operation's **write grants** = write scopes (env list, else a scopes file, one directory
   per line, else tool root + `/tmp`) ∪ tool-cache directories from a policy file (`bash_extra_write`) ∪ a fixed device
   set (`/dev/null`, `/dev/zero`, `/dev/full`, `/dev/tty`, `/dev/ptmx`, `/dev/pts`, `/dev/shm`, `/proc`).
2. Instead of `bash -c CMD` it executes a launcher: `python3 -S launcher --mode M --write D1 … -- /bin/bash -c CMD`.
3. The launcher (single-threaded) queries the Landlock ABI; builds a ruleset whose handled rights are the
   *write-class* filesystem rights only (write file, remove file/dir, make reg/dir/sym/fifo/sock/char/block, plus
   refer for ABI ≥ 2 and truncate for ABI ≥ 3) — read, execute and network are deliberately not handled, so they stay
   unrestricted; adds one path-beneath rule per grant (directories get all handled rights, regular files only
   write+truncate); sets no-new-privileges; restricts itself; then `execv`s bash. The confinement is inherited by every
   descendant and cannot be lifted.
4. If `TMPDIR` is unset and `/tmp` is granted, the launcher sets `TMPDIR=/tmp` (libraries such as SQLite otherwise put
   temp files in `/var/tmp` and fail).
5. Modes: `enforce` (the ruleset cannot be applied → exit 126, the command never runs), `best-effort` (run unconfined
   with a warning), `off`. The policy file is read on every call, so tightening or rolling back needs no restart.
6. On a failed confined operation whose output contains `Permission denied`, the result carries a hint naming the
   scopes and a log row is appended for the operator to review (a feedback loop for adding legitimate grants).
7. Known limit: opening `/dev/stdout` when stdout is a pipe resolves through `/proc/<pid>/fd` to a pipe inode outside
   any grant and is denied; duplicating descriptors (`>&1`) is unaffected. Measured on 55,010 real agent commands:
   4 used the denied form (0.007 %).

## 3. Operator-only, one-shot, operation-bound approvals
1. **Fingerprint.** For each operation the guard blocks, compute `sha256(canonical JSON of the operation minus
   scheduling fields)[:16]` — scheduling fields (timeouts, detach flags, log paths, the `allow_destructive` and
   `approval` fields) are excluded so the fingerprint names *what* runs, not how it is scheduled.
2. **Refusal.** The whole batch is refused (nothing runs); the message names the rule and the fingerprint; a pending
   record (fingerprint, operation text, rule, time) is written to the approval directory's `pending/` area.
3. **Minting (human only).** Two channels write a token file `<approval dir>/<fingerprint>.json` = {fingerprint, who,
   operation text, time}:
   - a terminal command that lists pending records and, for one fingerprint, shows the operation and reads `y/N`
     from the controlling terminal — it refuses to run without an interactive terminal, so an agent's pipe cannot use it;
   - the MCP server, on refusal, asks the **user** through the client's elicitation prompt (a form with one boolean),
     and only on an explicit accept writes the token itself and re-runs the call once.
4. **Unforgeable by the agent.** The approval directory lies outside every write scope (so `write`/`patch` ops are
   refused there) and outside every sandbox grant (so shell writes there fail in the kernel); a guard rule also blocks
   any command naming the terminal tool or the directory.
5. **Consumption.** On the next submission of the same operation the guard finds the token, deletes it, checks age
   ≤ 15 minutes, and lets that one operation run; a second submission is blocked again. The model-set flag alone is
   refused with an explanation.
6. Design notes: no password, no secondary "hacking examination" stage, no AI model rewriting or re-scoring the
   agent's action — approval is a human decision bound to an exact operation hash.

## 4. Whole-file guarantee and announced cuts
1. A read-modify-write tool (edit, append) reads with an explicit "full" flag and then **refuses to write** unless
   (a) the reader reports no truncation, (b) the bytes decoded as UTF-8 without substitution, and (c) the encoded
   length equals the file's size from `stat`. Measured motivation: an edit tool reading through a 128 KiB view wrote a
   159,381-byte source file back as 131,081 bytes and reported success.
2. Every capped output carries an in-band notice: bash stdout (`… [stdout cut at N of M chars …]`), file-list and
   search results (`… [first N of M …]`), multi-file reads, and batch results — where a batch is shrunk by
   water-filling the character budget across the per-operation fields (small results stay whole, large ones are cut
   head + tail and spilled in full to a file named in the notice).
3. Payloads reach the executor on standard input, never as one argument (a single argument is capped at 128 KiB on
   Linux), and a validation refusal is surfaced as the error text rather than as an empty result.

## 5. Measured (2026-10-06)
Guard selftest 62/62 (incl. flag-alone refused, token runs once, token not reusable, self-approval blocked); sandbox
tests 7 (outside write denied, full in-scope lifecycle, children inherit, detached path confined, extra grants,
off mode, enforce refuses without Landlock); MCP tests 51 (incl. 200 KB edit exact to the byte, approval accept /
decline / no-elicitation); launcher overhead ≈ 10 ms per operation.

## 6. Variations also disclosed
Grants from a per-project or per-user policy; network confinement (Landlock TCP rules) per operation class;
approval tokens signed with an operator key instead of filesystem placement; approvals scoped to a batch hash or to a
rule id with a count; a remote approval channel (chat, mobile push) writing the same token; macOS confinement via the
system sandbox profile with the same grant computation; denial logs aggregated to propose new grants for review.

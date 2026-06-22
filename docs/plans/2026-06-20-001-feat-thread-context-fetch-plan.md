---
title: "feat: Inject AgentMail thread context into dispatched Claude sessions"
date: 2026-06-20
type: feat
depth: lightweight
status: ready
---

# feat: Inject AgentMail thread context into dispatched Claude sessions

## Summary

The `cc-daemon.py` email dispatcher spawns a fresh, stateless Claude Code session
per inbound email and hands it a prompt file containing only the single message's
`From` / `Subject` / body. On a reply, the only prior-thread context the agent
sees is whatever the sender's email client happened to quote into the body —
fragile, lossy, and absent entirely if quoting is stripped.

This plan makes thread context **durable and authoritative** without changing the
stateless-session model: inject the AgentMail `thread_id` into the prompt and
instruct the dispatched agent to call the AgentMail MCP `get_thread` tool **on
demand** — only when the message is part of an ongoing thread — to reconstruct the
full history from the API before acting. The thread (held in AgentMail) becomes
the memory; each fresh session hydrates from it when needed.

Scope is deliberately narrow: one helper signature change, two call sites, unit
tests mirroring the existing suite, and a README note. The AgentMail path is the
only active backend and the only one wired for real thread fetching; the dormant
Primitive backend is left functionally untouched.

---

## Problem Frame

- **Stateless by design, and that stays.** Each email → fresh Claude session is a
  feature: clean context, no state bleed between unrelated tasks, a per-email
  audit trail. The fix must preserve this, not replace it with persistent agent
  memory.
- **The context source is the bug.** Today the agent's only window into prior
  turns is email-client quoting embedded in `msg.text`. This degrades with thread
  depth (nested `>>>>`), varies by client, and disappears when quoting is stripped.
- **The authoritative record already exists.** AgentMail holds the full thread and
  exposes it via the `get_thread` MCP tool. Every inbound message carries its
  `thread_id`. The dispatched session inherits the AgentMail MCP from the global
  `~/.claude.json`, so the tool is reachable. Nothing new needs to be built — the
  prompt just needs to point the agent at it.

---

## Requirements

- **R1.** On a reply / ongoing-thread message, the dispatched agent must be able
  to reconstruct full thread history from the AgentMail API rather than relying on
  email-client quoting.
- **R2.** On a first-contact email (single-message thread), the prompt must impose
  no wasted fetch — the instruction is conditional, a no-op when there is no prior
  history.
- **R3.** The stateless-session-per-email model is unchanged; no persistent agent
  memory is introduced.
- **R4.** The AgentMail dispatch path passes the real `thread_id`. The Primitive
  dispatch path passes no thread id and renders no thread block (functionally
  untouched).
- **R5.** Existing prompt content (From / Subject / body / attachment block) and
  all current gating, dispatch, and mark-read behavior are preserved.

---

## Key Technical Decisions

- **Inject via `build_prompt`, not the pointer line.** The dispatched agent reads
  the prompt *file*; the cmux/Ghostty pointer is just a one-line "go read this
  file" trigger and must stay single-line (`_cmux_send_pointer` rejects newlines).
  So the thread block belongs in `build_prompt`'s output, alongside the existing
  attachment block. No change to the dispatch/pointer path.

- **Conditional, on-demand fetch — not unconditional.** The block instructs the
  agent to call `get_thread` *only if the message is part of an ongoing thread*.
  A brand-new email is a single-message thread whose `get_thread` would return
  only the message already in the prompt, so the agent simply proceeds. This
  matches the "on demand" intent: the agent decides at runtime whether context is
  needed, and first-contact emails pay nothing.

- **Pass `inboxId` + `threadId` explicitly in the instruction.** `get_thread`
  requires both (`inboxId`, `threadId`). Both are available at dispatch time —
  `INBOX` (module global) and `msg.thread_id` — so the prompt can name the exact
  call, removing guesswork for the agent.

- **Graceful fallback to quoted text.** The block tells the agent that if
  `get_thread` is unavailable or errors (e.g. the AgentMail MCP needs
  organization selection / auth in that session), it should fall back to whatever
  quoted context is in the message body. This makes the feature strictly additive:
  worst case is today's behavior, never worse.

- **Optional `thread_id` parameter, defaulting to `None`.** Keeps the Primitive
  call site a trivial `thread_id=None` (no thread block) and keeps the change to
  `build_prompt` backward-compatible with the existing tests' positional calls.

---

## Implementation Units

### U1. Extend `build_prompt` with an optional thread-context block and wire both call sites

**Goal:** Render a conditional "load full thread via `get_thread`" instruction in
the prompt file when a thread id is present, and thread the value through from the
AgentMail handler.

**Requirements:** R1, R2, R3, R4, R5

**Dependencies:** none

**Files:**
- `cc-daemon.py` — modify `build_prompt` (currently lines ~344–363); update the two
  call sites in `handle_message` (~line 549) and `handle_primitive_email` (~line 631)
- `test_cc_daemon.py` — covered in U2

**Approach:**
- Add an optional `thread_id: str | None = None` parameter to `build_prompt`
  (keep it last so existing positional calls keep working).
- When `thread_id` is truthy, render a thread-context block. Place it **after the
  intro sentence and before `From:`** so the agent reads the context instruction
  before the task. Directional shape of the block (exact wording is the
  implementer's call, not spec):

  > *This message is part of AgentMail thread `{thread_id}` in inbox `{INBOX}`.
  > If it is a reply or part of an ongoing conversation, first call the AgentMail
  > MCP `get_thread` tool with `inboxId="{INBOX}"` and `threadId="{thread_id}"`
  > to load the full thread history for context before acting. The message below
  > is the latest in the thread and contains the actual task. If `get_thread` is
  > unavailable or errors, fall back to any quoted text in the body.*

- When `thread_id` is falsy, render nothing (current output, byte-for-byte).
- In `handle_message`, pass `thread_id=msg.thread_id` (confirmed present on the
  AgentMail `Message` model). In `handle_primitive_email`, pass `thread_id=None`.

**Patterns to follow:** Mirror the existing optional `attach_block` construction in
`build_prompt` — build the block string conditionally, interpolate it into the
final f-string. Keep `%`-style logging and existing structure untouched.

**Test scenarios:** covered in U2 (kept as a separate unit so the behavior change
and its coverage land as reviewable, atomic commits).

**Verification:** `build_prompt(..., thread_id="thr_x")` includes the thread id,
the inbox, and `get_thread`; `build_prompt(..., thread_id=None)` is identical to
the pre-change output; the daemon imports and the existing test suite still passes.

---

### U2. Unit-test the thread-context behavior

**Goal:** Lock in the conditional block, the no-op default, and the preserved
existing output.

**Requirements:** R1, R2, R5

**Dependencies:** U1

**Files:**
- `test_cc_daemon.py` — add tests in the existing `# --- build_prompt ---` section
  (after line ~84), mirroring `test_build_prompt_includes_headers_and_body` and
  `test_build_prompt_lists_attachment_paths`

**Approach:** Pure-function assertions on `build_prompt`'s returned string — no
mocks needed, same style as the two existing build_prompt tests.

**Test scenarios:**
- **Thread block present when `thread_id` given:** `build_prompt("a@b.com", "subj",
  "body", [], thread_id="thr_123")` → output contains `thr_123`, the inbox value,
  and `get_thread`. Asserts R1.
- **Conditional phrasing is present:** the rendered block contains the
  reply/ongoing-conversation conditional wording (assert on a stable substring like
  `get_thread` plus `thread`), so a future edit that makes the fetch unconditional
  fails the test. Asserts R2.
- **No thread block when `thread_id` omitted/None:** `build_prompt("a@b.com",
  "subj", "body", [])` and `build_prompt(..., thread_id=None)` → output does **not**
  contain `get_thread` or `thread_id`, and equals the existing expected headers+body
  output. Asserts R2, R5.
- **Thread block coexists with attachments:** `build_prompt(..., atts,
  thread_id="thr_9")` → output contains both the attachment path / `use the Read
  tool` text **and** the `get_thread` instruction. Asserts the two optional blocks
  don't clobber each other.
- **Existing build_prompt tests still pass unchanged:** positional 4-arg calls in
  the current tests continue to work (regression guard on the signature change).

**Verification:** `./venv/bin/python -m pytest test_cc_daemon.py` is green,
including the two pre-existing build_prompt tests.

---

### U3. Document the thread-context behavior in the README

**Goal:** Record that dispatched sessions reconstruct thread history on demand via
`get_thread`, and the prerequisite that the AgentMail MCP is reachable in the
session.

**Requirements:** R1

**Dependencies:** U1

**Files:**
- `README.md` — add a short subsection (near the existing behavior / AgentMail
  description) explaining the thread-context injection and its fallback

**Approach:** A few sentences: replies cause the dispatched agent to call the
AgentMail MCP `get_thread` for full history; first-contact emails skip it; if the
AgentMail MCP isn't connected/authenticated in the session, the agent falls back to
quoted body text. Note the dependency: the AgentMail MCP must be present in the
session's Claude config (it is, via `~/.claude.json`).

**Test scenarios:** `Test expectation: none — documentation-only change.`

**Verification:** README renders; the new subsection accurately states the
conditional-fetch behavior and the MCP prerequisite.

---

## Risks & Dependencies

- **AgentMail MCP auth in the dispatched session (primary risk).** `get_thread`
  only works if the freshly-launched Claude session has the AgentMail MCP connected
  *and* an organization selected. The MCP is in `~/.claude.json` so it loads, but
  it may require `select_organization` / `auth_me` before `get_thread` succeeds.
  **Mitigation:** the prompt block's explicit fallback-to-quoted-text instruction
  means an auth failure degrades to today's behavior, not a hard failure. Confirm
  real behavior in the live verification below; if auth proves flaky, a follow-up
  could have the daemon itself fetch the thread and inline it (deferred — see below).

- **Prompt-length growth.** The block adds a few lines to every threaded prompt;
  negligible against email bodies and attachments.

- **No change to dispatch reliability.** The cmux readiness/await path, allowlist
  gate, attachment download, and mark-read logic are untouched.

---

## Verification (post-implementation, live)

1. Run the unit suite: `./venv/bin/python -m pytest test_cc_daemon.py` — green.
2. Restart the daemon, then **reply** to an existing Pua thread with a task that
   depends on prior-message context (e.g. "do the thing we discussed above" with
   the actual detail only in an earlier message).
3. Confirm in the dispatched session that the agent called `get_thread` and acted
   on context from an earlier message — not just the latest body. Check
   `~/.agentmail-cc/dispatch.log` shows normal dispatch and the prompt file under
   `~/.agentmail-cc/prompts/` contains the thread block.
4. Send a **fresh** (non-reply) email and confirm the agent proceeds without a
   wasted `get_thread` round-trip.

---

## Scope Boundaries

**In scope:** conditional on-demand thread fetch via the prompt; AgentMail path;
unit tests; README note.

### Deferred to Follow-Up Work

- **Daemon-side thread inlining.** If MCP auth in dispatched sessions proves
  unreliable, have the daemon call AgentMail's thread API directly and write the
  rendered history into the prompt file (no dependence on the session's MCP). More
  robust, more code — only worth it if the MCP path disappoints in verification.
- **Primitive thread support.** Out of scope; the backend is dormant and its API
  doesn't expose threads the same way. Revisit only if Primitive is ever activated.
- **Deleting the dormant Primitive backend** to simplify `cc-daemon.py` — a
  separate cleanup, unrelated to this feature.

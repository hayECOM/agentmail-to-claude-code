#!/usr/bin/env python3
"""Payment lane — the cortana.h qualify-and-spawn gate for the Mercury flow.

This is lane 2 of the daemon (see cc-daemon.py). It watches cortana.h@agentmail.to
and, for a *qualifying* Mercury GPO payment email, spawns a **Cortana** cmux
session (identity booted via CLAUDE_LAUNCH_CWD=~/Cortana) briefed to run the
`logging mercury GPO payment` playbook. Non-qualifying mail is ignored + logged.

This module ONLY gates and spawns. The payment/reconciliation/Airtable logic
lives in the vault playbook (single source of truth) — never here.

A qualifying email must satisfy all three:
  1. recipient matches finance+<code>@staygoldenhi.com OR the lane-2 inbox's own
     plus-address cortana.h+<code>@agentmail.to   (code = dedup/PO anchor)
  2. subject matches "<payor> sent you $X"
  3. DKIM from an accepted signing domain is verifiable from the raw headers

The recipient gate accepts two shapes. The SG alias finance+<code>@staygoldenhi.com
rides in on a Gmail auto-forward (the envelope was rewritten, so the true recipient
survives in Delivered-To/X-Forwarded-To/X-Original-To). The lane-2 plus-address
cortana.h+<code>@agentmail.to is the DIRECT path — Mercury sends straight to the
AgentMail inbox and the To header survives intact. Its base is derived from
CC_LANE2_INBOX (local part + domain), never hardcoded, so there's no second env var.

On a Gmail auto-forward the surviving DKIM pass is the ORIGIN domain, not the
staygoldenhi.com re-sign: Mercury signs d=mg.mercury.com (selector pic), and
Google's Authentication-Results carries it through as dkim=pass
header.i=@mg.mercury.com. So the gate accepts the staygoldenhi.com alias AND
Mercury's own signing domains (_ACCEPTED_DKIM_DOMAINS) — a direct spoof to
cortana.h can produce none of them. The DKIM check is layered and *fails closed*,
and if AgentMail exposes no auth headers it logs prominently and gates on
recipient + subject + AgentMail's own auth verdict (per the plan's fallback).

The pure functions (extract_finance_code / parse_payment_subject / dkim_check /
evaluate) take plain values so they unit-test without the AgentMail SDK.
"""

from __future__ import annotations

import logging
import os
import pathlib
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field

log = logging.getLogger("cc-mail")

# --- config (env-overridable, mirrors cc-daemon.py style) --------------------

CORTANA_REPO = os.environ.get("CC_CORTANA_REPO", "/Users/hayecom/Cortana")
SPAWN_CODER = os.environ.get(
    "SPAWN_CODER",
    str(pathlib.Path.home() / "Workspace" / "tools" / "spawn-coder" / "spawn-coder.sh"),
)
PROMPT_ROOT = pathlib.Path(
    os.environ.get("CC_HOME", pathlib.Path.home() / ".agentmail-cc")
) / "payment-prompts"
# Which Telegram bucket the playbook posts the write-set / escalations to.
TELEGRAM_TOPIC = os.environ.get("CC_LANE2_TOPIC", "stay_golden")
# Where near-miss pings land: a *payment-shaped* email that failed one gate layer
# (e.g. DKIM showed an unrecognized origin domain on auto-forwarded mail) would
# otherwise die in the log. It goes to the operator's own 'cortana' topic — NOT
# stay_golden — because it's a tuning signal, not a payment to action.
NEAR_MISS_TOPIC = os.environ.get("CC_LANE2_NEAR_MISS_TOPIC", "cortana")
TG_SEND = os.environ.get(
    "TG_SEND",
    str(pathlib.Path.home() / "Workspace" / "tools" / "telegram-gateway" / "tg-send"),
)
# tg-send is a fast local post; don't share the 5-minute spawn budget with it.
TG_SEND_TIMEOUT_S = int(os.environ.get("CC_LANE2_PING_TIMEOUT", "30"))
PLAYBOOK = "logging mercury GPO payment"
SPAWN_TIMEOUT_S = int(os.environ.get("CC_LANE2_SPAWN_TIMEOUT", "300"))
# The lane-2 inbox (mirrors cc-daemon.py's LANE2_INBOX). The accepted direct-send
# recipient shape is DERIVED from this — cortana.h@agentmail.to admits
# cortana.h+<code>@agentmail.to — so there is no separate recipient env var.
LANE2_INBOX = os.environ.get("CC_LANE2_INBOX", "").strip().lower()

# --- gate regexes ------------------------------------------------------------

_RECIP_RE = re.compile(r"finance\+([A-Za-z0-9._-]+)@staygoldenhi\.com", re.I)
# "<payor> sent you $4,200.00" — payor is everything before " sent you ".
_SUBJECT_RE = re.compile(
    r"^\s*(?P<payor>.+?)\s+sent\s+you\s+\$?\s*(?P<amount>[\d,]+(?:\.\d{1,2})?)",
    re.I,
)
# A Mercury auto-forward preserves the original subject verbatim; a Re:/Fwd: prefix
# only shows up on a human reply or manual forward — not the real-time notification.
# (Live 2026-07-04 16:46: a Gmail reply parsed as a payment and cost a spawn.)
_REPLY_FWD_PREFIX_RE = re.compile(r"^\s*(?:re|fwd)\s*:", re.I)
# Header names (case-insensitive) that can carry the true recipient through a
# Gmail auto-forward. `to`/`cc` come from the parsed message; the X-/Delivered
# forms survive forwarding when the envelope recipient has been rewritten.
_RECIPIENT_HEADER_KEYS = (
    "delivered-to", "x-forwarded-to", "x-original-to", "to", "cc",
)
_SAFE_CASE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _ci_headers(headers: dict | None) -> dict:
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


def _lane2_recip_re() -> re.Pattern | None:
    """Boundary-anchored recipient regex for the lane-2 inbox's plus-address,
    DERIVED from CC_LANE2_INBOX (no separate env var): cortana.h@agentmail.to
    admits cortana.h+<code>@agentmail.to. Returns None when the inbox is unset or
    has no '@' (lane 2 disabled / malformed) — the finance+ shape still applies.

    Anchored on BOTH sides in the PR-8 posture: the local part must sit on a
    local-part boundary (so notcortana.h+cle@... and cortana.h.evil+cle@... do NOT
    match) and the domain must end on a domain boundary (so ...@agentmail.to.evil.com
    and ...@evilagentmail.to do NOT match). re.escape guards the local part's dot so
    it can't behave as a wildcard.
    """
    local, sep, domain = LANE2_INBOX.partition("@")
    if not sep or not local or not domain:
        return None
    return re.compile(
        rf"(?:^|[^A-Za-z0-9._+-]){re.escape(local)}\+([A-Za-z0-9._-]+)"
        rf"@{re.escape(domain)}(?![A-Za-z0-9.-])",
        re.I,
    )


def extract_finance_code(recipients: list[str] | None, headers: dict | None) -> str | None:
    """Return <code> if any recipient matches an accepted plus-address shape.

    Two shapes qualify: the SG alias finance+<code>@staygoldenhi.com (survives a
    Gmail auto-forward via Delivered-To/X-Forwarded-To/X-Original-To) AND the lane-2
    inbox's own plus-address, derived from CC_LANE2_INBOX
    (cortana.h@agentmail.to -> cortana.h+<code>@agentmail.to) for Mercury's direct
    sends. Both are boundary-anchored (see _lane2_recip_re / PR-8 posture).
    """
    h = _ci_headers(headers)
    candidates: list[str] = list(recipients or [])
    for key in _RECIPIENT_HEADER_KEYS:
        if h.get(key):
            candidates.append(h[key])
    patterns = [_RECIP_RE]
    lane2 = _lane2_recip_re()
    if lane2 is not None:
        patterns.append(lane2)
    for c in candidates:
        for pat in patterns:
            m = pat.search(c or "")
            if m:
                return m.group(1)
    return None


def parse_payment_subject(subject: str | None) -> tuple[str, str] | None:
    """(payor, amount) from "<payor> sent you $X", or None.

    A Re:/Fwd: prefix disqualifies the subject: an auto-forwarded Mercury payment
    keeps its original subject, so those prefixes mark a human reply or manual
    forward — not the notification we spawn a session on.
    """
    s = subject or ""
    if _REPLY_FWD_PREFIX_RE.match(s):
        return None
    m = _SUBJECT_RE.search(s)
    if not m:
        return None
    payor = m.group("payor").strip()
    amount = m.group("amount").replace(",", "")
    if not payor:
        return None
    return payor, amount


@dataclass
class DkimResult:
    verdict: str          # "pass" | "fail" | "unknown"
    strength: str         # "strong" | "medium" | "present-no-match" | "unavailable"
    authed: bool          # AgentMail's own auth verdict (not the DKIM domain check)
    detail: str


# DKIM signing domains that prove a genuine Mercury GPO payment notification. On a
# Gmail auto-forward the surviving DKIM pass is the ORIGIN domain — Mercury signs
# d=mg.mercury.com (selector pic), not the staygoldenhi.com re-sign — so we accept
# the SG alias AND Mercury's own signing domains. Bare mercury.com is kept as a
# defensive catch in case a forwarding path reports the org domain instead of the
# mg. subdomain. See README (lane 2).
_ACCEPTED_DKIM_DOMAINS = ("staygoldenhi.com", "mg.mercury.com", "mercury.com")


def _accepted_dkim_domain(text: str) -> str | None:
    """First accepted DKIM domain appearing in `text` at a domain boundary, else
    None. Anchored on both sides so a look-alike (evilmercury.com,
    mercury.company.com) can't slip past what a bare substring would wave through.
    `text` is expected already lowercased.
    """
    for d in _ACCEPTED_DKIM_DOMAINS:
        if re.search(rf"(?:^|[^a-z0-9.-]){re.escape(d)}(?![a-z0-9.-])", text):
            return d
    return None


def dkim_check(headers: dict | None, labels: list[str] | None, event_type: str) -> DkimResult:
    """Verify DKIM from an accepted signing domain, layered + fail-closed.

    Accepted domains (_ACCEPTED_DKIM_DOMAINS): the staygoldenhi.com re-sign AND
    Mercury's own signing domains — on a Gmail auto-forward the surviving DKIM pass
    is the ORIGIN (d=mg.mercury.com), not the SG alias.

    Preference: (1) an Authentication-Results clause with dkim=pass AND an accepted
    domain; else (2) a DKIM-Signature carrying d=<accepted domain>, trusted only if
    AgentMail also marked the message authenticated; else (3) unknown — no usable
    auth headers exposed (caller logs + gates on the rest).
    """
    h = _ci_headers(headers)
    labels = [str(x).lower() for x in (labels or [])]
    authed = "unauthenticated" not in labels and event_type != "message.received.unauthenticated"

    ar = h.get("authentication-results") or h.get("arc-authentication-results") or ""
    if ar:
        for clause in ar.split(";"):
            c = clause.strip().lower()
            if re.search(r"\bdkim\s*=\s*pass\b", c):
                matched = _accepted_dkim_domain(c)
                if matched:
                    return DkimResult("pass", "strong", authed,
                                      f"Authentication-Results dkim=pass {matched}")

    sig = (h.get("dkim-signature") or "").replace(" ", "").lower()
    sig_domain = next(
        (d for d in _ACCEPTED_DKIM_DOMAINS
         if re.search(rf"(?:^|;)d={re.escape(d)}(?:;|$)", sig)),
        None,
    )
    if sig_domain:
        if authed:
            return DkimResult("pass", "medium", authed,
                              f"DKIM-Signature d={sig_domain} + AgentMail authenticated")
        return DkimResult("fail", "medium", authed,
                          f"DKIM-Signature d={sig_domain} but AgentMail flagged unauthenticated")

    if not ar and not sig:
        return DkimResult("unknown", "unavailable", authed,
                          "no Authentication-Results / DKIM-Signature headers exposed by AgentMail")

    # Auth headers present but no accepted DKIM pass -> spoof/misalign, reject.
    return DkimResult("fail", "present-no-match", authed,
                      f"auth headers present but no accepted dkim=pass domain (ar={ar[:120]!r})")


@dataclass
class Decision:
    qualified: bool
    code: str | None
    payor: str | None
    amount: str | None
    dkim: DkimResult
    case_ref: str | None
    reasons: list[str] = field(default_factory=list)


def _case_ref(code: str) -> str:
    return ("MERC-" + _SAFE_CASE_RE.sub("-", code))[:48]


def evaluate(
    recipients: list[str] | None,
    headers: dict | None,
    subject: str | None,
    labels: list[str] | None,
    event_type: str,
) -> Decision:
    """Combine the three gate layers into a qualify/ignore decision."""
    reasons: list[str] = []
    code = extract_finance_code(recipients, headers)
    subj = parse_payment_subject(subject)
    dk = dkim_check(headers, labels, event_type)

    if code is None:
        reasons.append("recipient does not match finance+<code>@staygoldenhi.com")
    if subj is None:
        reasons.append('subject does not match "<payor> sent you $X"')

    if dk.verdict == "pass":
        dkim_ok = True
    elif dk.verdict == "unknown":
        # Fallback path (plan §Email authentication): no auth headers exposed, so
        # gate on the rest + AgentMail's own verdict. Caller logs this prominently.
        dkim_ok = dk.authed
        reasons.append(
            f"DKIM headers unavailable ({dk.detail}); gating on recipient+subject+"
            f"AgentMail-authenticated(authed={dk.authed})"
        )
    else:
        dkim_ok = False
        reasons.append(f"DKIM check failed ({dk.detail})")

    payor, amount = subj if subj else (None, None)
    qualified = code is not None and subj is not None and dkim_ok
    return Decision(
        qualified=qualified,
        code=code,
        payor=payor,
        amount=amount,
        dkim=dk,
        case_ref=_case_ref(code) if code else None,
        reasons=reasons,
    )


# --- near-miss ping ----------------------------------------------------------

def is_payment_shaped(decision: Decision) -> bool:
    """True if a *non-qualifying* email still looks like a Mercury payment.

    Payment-shaped = the subject parsed as "<payor> sent you $X" (payor is set)
    OR a recipient/header matched an accepted plus-address — either the SG alias
    finance+<code>@staygoldenhi.com or the lane-2 direct-send shape
    cortana.h+<code>@agentmail.to — so decision.code is set. Either one is enough: a
    real payment that trips a single gate layer (e.g. DKIM showing an unrecognized
    origin domain) is worth surfacing for tuning. Mail matching neither is an
    unsolicited probe — there's no oracle for it, so it stays silent.
    """
    return decision.code is not None or decision.payor is not None


def ping_near_miss(decision: Decision, subject: str) -> bool:
    """Post a terse near-miss ping to the 'cortana' topic so a payment-shaped email
    that didn't qualify surfaces for tuning instead of dying in the log.

    No-op for non-payment-shaped mail (probes get no oracle). Returns True only if a
    ping was actually sent — the daemon logs that alongside its LANE2 IGNORE line.
    """
    if not is_payment_shaped(decision):
        return False
    if not os.access(TG_SEND, os.X_OK):
        log.error("LANE2 near-miss ping skipped: tg-send not executable: %s", TG_SEND)
        return False

    reasons = "; ".join(decision.reasons) or "(no reasons recorded)"
    text = (
        f"⚠️ Mercury near-miss: payment-shaped mail did NOT qualify.\n"
        f"subject: {subject or '(no subject)'}\n"
        f"reasons: {reasons}"
    )
    cmd = [TG_SEND, "--topic", NEAR_MISS_TOPIC, "--text", text]
    log.info("LANE2 near-miss ping topic=%s subj=%r", NEAR_MISS_TOPIC, subject)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TG_SEND_TIMEOUT_S)
    except Exception as e:
        log.error("LANE2 near-miss ping invocation failed: %s", e)
        return False
    if r.returncode != 0:
        log.error("LANE2 near-miss ping rc=%s stderr=%s",
                  r.returncode, (r.stderr or "")[:300])
        return False
    return True


# --- Cortana spawn -----------------------------------------------------------

def build_cortana_prompt(
    decision: Decision,
    raw_from: str,
    subject: str,
    body: str,
    thread_id: str | None,
    inbox: str,
) -> str:
    """The brief written to a file and handed to the spawned Cortana session."""
    thread_block = ""
    if thread_id:
        thread_block = (
            f"This message is AgentMail thread {thread_id} in inbox {inbox}. Call "
            f"the AgentMail MCP get_thread tool (inboxId=\"{inbox}\", "
            f"threadId=\"{thread_id}\") to load full thread history before acting.\n\n"
        )
    tg_guidance = (
        f"Post the write-set / escalations to the Telegram '{TELEGRAM_TOPIC}' topic "
        f"using this exact case ref so Rony's taps route back to THIS session:\n"
        f"  {TG_SEND} --topic {TELEGRAM_TOPIC} --case-ref {decision.case_ref} "
        f"--spawn-repo {CORTANA_REPO} --text \"...\" "
        f"--button \"Approve:approve\" --button \"Reject:reject:destructive\"\n"
        f"(During the supervised rollout: post the full intended write-set with "
        f"Approve/Reject and write only on Approve. Once autonomous: buttons only "
        f"on escalations.)\n"
    )
    return (
        f"A qualifying Mercury GPO payment email arrived at {inbox}. You are "
        f"Cortana. Run the vault playbook end to end for this email.\n\n"
        f"TASK: Read [[{PLAYBOOK}]] from Rony's Brain and run it for this payment. "
        f"The playbook is the single source of truth for parse → reconcile → "
        f"write/escalate → Airtable. Do NOT improvise the money logic.\n\n"
        f"Case ref: {decision.case_ref}\n"
        f"Parsed by the gate (verify against the body): payor={decision.payor!r}, "
        f"amount=${decision.amount}, finance code={decision.code!r}\n"
        f"DKIM gate: {decision.dkim.verdict} ({decision.dkim.strength}) — "
        f"{decision.dkim.detail}\n\n"
        f"{thread_block}"
        f"From: {raw_from}\n"
        f"Subject: {subject}\n\n"
        f"{body}\n\n"
        f"---\n"
        f"{tg_guidance}"
    )


def spawn_cortana(decision: Decision, prompt_text: str) -> str | None:
    """Write the brief to a file and spawn a Cortana cmux session on it.

    Reuses spawn-coder.sh: --repo ~/Cortana sets CLAUDE_LAUNCH_CWD so Cortana's
    identity boots (NOT Roland). Returns the workspace handle, or None on failure.
    """
    PROMPT_ROOT.mkdir(parents=True, exist_ok=True)
    ref = decision.case_ref or "MERC-unknown"
    prompt_path = PROMPT_ROOT / f"{ref}.md"
    prompt_path.write_text(prompt_text)

    if not os.access(SPAWN_CODER, os.X_OK):
        log.error("LANE2 spawn-coder not executable: %s", SPAWN_CODER)
        return None

    brief = (
        f"A qualifying Mercury payment email arrived at cortana.h (case {ref}). "
        f"Read and run the playbook described in this file: {prompt_path}"
    )
    cmd = [
        SPAWN_CODER,
        "--repo", CORTANA_REPO,
        "--brief", brief,
        "--title", f"Cortana: mercury {ref}",
        # spawn-coder defaults to the Roland group; these are Cortana sessions
        "--group", "Cortana",
    ]
    # THIS LANE STAYS ON THE PERSONAL SEAT. Since 2026-08-19 `spawn-coder.sh` spawns
    # sessions on the Stay Golden team profile by default, which is right for a coder a
    # human dispatched and watches — and wrong here twice over: this is a CORTANA session,
    # and it fires from the always-on com.agentmail.cc daemon with nobody looking. An
    # expired team login would boot it into a sign-in prompt, the ready poll would fail,
    # and the handle spawn-coder still echoes on that path would let the caller below mark
    # a MERCURY PAYMENT email read. Passed as $SC_SEAT rather than `--seat personal` so an
    # older spawn-coder (which would reject the unknown flag and kill the spawn outright)
    # simply ignores it.
    env = {**os.environ, "SC_SEAT": "personal"}
    log.info("LANE2 spawning cortana: %s", " ".join(shlex.quote(c) for c in cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=SPAWN_TIMEOUT_S, env=env)
    except Exception as e:
        log.error("LANE2 spawn-coder invocation failed: %s", e)
        return None
    handle = None
    for ln in reversed((r.stdout or "").splitlines()):
        m = re.search(r"workspace:\d+", ln)
        if m:
            handle = m.group(0)
            break
    if not handle:
        log.error("LANE2 spawn-coder no handle (rc=%s stderr=%s)",
                  r.returncode, (r.stderr or "")[:300])
    return handle


if __name__ == "__main__":
    # Tiny CLI probe: pipe a raw subject/recipient to see the gate decision.
    print("payment_lane gate self-check", file=sys.stderr)
    d = evaluate(
        ["finance+po1183@staygoldenhi.com"],
        {"Authentication-Results": "mx.google.com; dkim=pass header.i=@staygoldenhi.com"},
        "Acme Corp sent you $4,200.00",
        [], "message.received",
    )
    print(d)

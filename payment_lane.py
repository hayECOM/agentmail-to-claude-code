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
cortana.h can produce none of them.

The DKIM check is layered and *fails closed* (final posture after review r2):
  - Only an Authentication-Results stamped by a trusted receiver (authserv-id
    amazonses.com or mx.google.com) is ever read, on the parsed and raw paths alike.
    ARC-Authentication-Results and other authserv-ids are ignored — a sender can
    write those. Pass requires a dkim=pass clause (outside comments) whose parsed
    signing domain — header.d, else the domain part of header.i — is exactly an
    accepted domain.
  - If AgentMail's parsed headers carry no trusted AR, the decision re-runs over
    the raw .eml (an injected fetcher; source="raw-fallback") before any
    DKIM-Signature presence is considered. A raw message with no trusted AR is a
    fail (no-receiver-ar); a fetch that fails twice is a fail (fetch-failed). Either
    lands in the near-miss ping for human eyes — nothing qualifies on AgentMail's
    generic auth bit.
  - Signature presence (+ AgentMail's auth bit) survives only as the terminal tier
    for callers that inject no raw fetcher; "no auth headers at all" never qualifies.

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
import time
from dataclasses import dataclass, field
from email.parser import BytesHeaderParser
from typing import Callable

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
    # "strong" (trusted receiver AR) | "medium" (signature presence, legacy no-raw
    # callers only) | "present-no-match" | "no-receiver-ar" (raw message carries no
    # trusted AR) | "fetch-failed" (raw fetch failed twice) | "unavailable"
    strength: str
    authed: bool          # AgentMail's own auth verdict (not the DKIM domain check)
    detail: str
    source: str = "parsed"  # "parsed" (AgentMail's header view) | "raw-fallback" (the .eml)


# DKIM signing domains that prove a genuine Mercury GPO payment notification. On a
# Gmail auto-forward the surviving DKIM pass is the ORIGIN domain — Mercury signs
# d=mg.mercury.com (selector pic), not the staygoldenhi.com re-sign — so we accept
# the SG alias AND Mercury's own signing domains. Bare mercury.com is kept as a
# defensive catch in case a forwarding path reports the org domain instead of the
# mg. subdomain. See README (lane 2).
_ACCEPTED_DKIM_DOMAINS = ("staygoldenhi.com", "mg.mercury.com", "mercury.com")

# The receivers whose Authentication-Results we trust, on BOTH paths: AgentMail's
# SES (the final hop, always stamps) and Google (the auto-forward hop). An AR with
# any other authserv-id was written by someone we don't run (review r1 F2, r2 X2).
_TRUSTED_AUTHSERV_IDS = ("amazonses.com", "mx.google.com")

# Raw-fetch retry: one retry after a short pause, then fail closed (review r2 X3).
RAW_FETCH_RETRY_DELAY_S = float(os.environ.get("CC_LANE2_RAW_RETRY_DELAY", "2"))


def _strip_comments(value: str) -> str:
    """`value` with every RFC 5322 comment (parenthesized CFWS, nestable, with
    backslash quoted-pairs) replaced by a space. Quoted strings are copied
    verbatim — a '(' inside one is not a comment. Review r2 X1: a sender-chosen
    comment like `dkim=fail (dkim=pass header.i=@mg.mercury.com)` must not count."""
    out: list[str] = []
    depth = 0
    in_quote = False
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and (depth or in_quote):
            if not depth:
                out.append(value[i:i + 2])
            i += 2
            continue
        if depth:
            depth += ch == "("
            depth -= ch == ")"
            if not depth:
                out.append(" ")
        elif in_quote:
            out.append(ch)
            in_quote = ch != '"'
        elif ch == "(":
            depth = 1
        else:
            out.append(ch)
            in_quote = ch == '"'
        i += 1
    return "".join(out)


def _split_unquoted(value: str, sep: str) -> list[str]:
    """Split on `sep` outside double-quoted strings."""
    parts, cur, in_quote, i = [], [], False, 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and in_quote:
            cur.append(value[i:i + 2])
            i += 2
            continue
        if ch == '"':
            in_quote = not in_quote
        if ch == sep and not in_quote:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


# One `name=value` pair of a resinfo clause; a quoted value is consumed whole so
# its interior is never read as further properties.
_PAIR_RE = re.compile(r'\s*([A-Za-z0-9_.-]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^\s"]*)')


def _dkim_pass_domain(clause: str) -> str | None:
    """The authenticated signing domain of one comment-stripped AR clause when it is
    a `dkim=pass` result, else None.

    The methodspec must open the clause (RFC 8601 resinfo), so `dkim=pass` sitting
    in some other property's value doesn't count. The domain is `header.d=`, else
    the DOMAIN PART (after the last '@') of `header.i=` — the AUID local part is
    free-form, so `header.i=mercury.com@attacker.example` is attacker.example
    (review r2 X1).
    """
    pairs: list[tuple[str, str]] = []
    pos = 0
    while pos < len(clause):
        m = _PAIR_RE.match(clause, pos)
        if m:
            v = m.group(2)
            if v.startswith('"'):
                v = re.sub(r"\\(.)", r"\1", v[1:-1])
            pairs.append((m.group(1).lower(), v))
            pos = m.end()
        else:
            nxt = re.compile(r"\s*\S+").match(clause, pos)
            if not nxt:
                break
            pairs.append(("", nxt.group(0).strip()))
            pos = nxt.end()
    if not pairs or pairs[0][0] != "dkim" or pairs[0][1].lower() != "pass":
        return None
    props = dict(reversed(pairs[1:]))  # first occurrence wins
    if props.get("header.d"):
        domain = props["header.d"]
    elif "@" in props.get("header.i", ""):
        domain = props["header.i"].rsplit("@", 1)[1]
    else:
        return None
    return domain.strip().lower().rstrip(".") or None


def _authserv_id(ar_value: str) -> str:
    """The authserv-id of an Authentication-Results value: its first token, before
    the first ';' (RFC 8601 §2.2 — an optional version may follow the id)."""
    head = ar_value.split(";", 1)[0].split()
    return head[0].lower() if head else ""


def _dkim_from_ar(ar: str, authed: bool) -> DkimResult:
    """Decide over ONE trusted-receiver Authentication-Results value. It is
    AUTHORITATIVE: no clause whose parsed signing domain is exactly an accepted
    domain means fail, full stop (review r1 F1)."""
    clauses = _split_unquoted(_strip_comments(ar), ";")[1:]  # [0] is the authserv-id
    for clause in clauses:
        domain = _dkim_pass_domain(clause)
        if domain in _ACCEPTED_DKIM_DOMAINS:
            return DkimResult("pass", "strong", authed,
                              f"Authentication-Results dkim=pass {domain}")
    return DkimResult("fail", "present-no-match", authed,
                      f"Authentication-Results carries no accepted dkim=pass domain "
                      f"(ar={ar[:120]!r})")


def _dkim_from_signature(h: dict, authed: bool) -> DkimResult:
    """The legacy terminal tier — ONLY for callers with no raw fetch. Signatures are
    never verified cryptographically here (presence only)."""
    sig = (h.get("dkim-signature") or "").replace(" ", "").lower()
    if not sig:
        return DkimResult("unknown", "unavailable", authed,
                          "no trusted Authentication-Results / DKIM-Signature headers "
                          "exposed by AgentMail")
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

    # A signature from some other domain -> spoof/misalign, reject.
    return DkimResult("fail", "present-no-match", authed,
                      "DKIM-Signature present but no accepted signing domain")


def raw_auth_headers(raw: bytes | str) -> dict:
    """Pull the one trusted Authentication-Results out of a raw RFC-822 message.

    Returns a dict shaped like AgentMail's parsed `headers` — either
    {"Authentication-Results": ...} or {} — so the same decision runs over it.

    Scans top-down and keeps the FIRST AR whose authserv-id is one of our actual
    receivers (_TRUSTED_AUTHSERV_IDS); SES prepends its stamp, so in real mail that
    is the topmost header. An AR with any other authserv-id is ignored, as is every
    ARC-Authentication-Results (a sender can mint its own ARC set at any i=) and
    every DKIM-Signature (presence is not verification). Review r1 F1/F2.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    msg = BytesHeaderParser().parsebytes(raw)
    for v in msg.get_all("Authentication-Results") or []:
        value = " ".join(str(v).split())
        if _authserv_id(value) in _TRUSTED_AUTHSERV_IDS:
            return {"Authentication-Results": value}
    return {}


RawFetcher = Callable[[], "bytes | str | None"]


def _fetch_raw(raw_fetch: RawFetcher) -> tuple[bytes | str | None, str]:
    """Call `raw_fetch`, retrying once after RAW_FETCH_RETRY_DELAY_S on a raise or
    an empty result. Returns (raw, "") on success, (None, why) when both fail."""
    why = ""
    for attempt in (1, 2):
        if attempt == 2:
            time.sleep(RAW_FETCH_RETRY_DELAY_S)
        try:
            raw = raw_fetch()
        except Exception as e:
            why = f"{type(e).__name__}: {str(e)[:120]}"
            continue
        if raw:
            return raw, ""
        why = "returned nothing"
    return None, why


def dkim_check(
    headers: dict | None,
    labels: list[str] | None,
    event_type: str,
    raw_fetch: RawFetcher | None = None,
) -> DkimResult:
    """Verify DKIM from an accepted signing domain, layered + fail-closed.

    Accepted domains (_ACCEPTED_DKIM_DOMAINS): the staygoldenhi.com re-sign AND
    Mercury's own signing domains — on a Gmail auto-forward the surviving DKIM pass
    is the ORIGIN (d=mg.mercury.com), not the SG alias.

    Only an Authentication-Results stamped by a trusted receiver
    (_TRUSTED_AUTHSERV_IDS) is ever read, on either path, and it is authoritative:
    pass only when a `dkim=pass` clause's parsed signing domain (header.d, else the
    domain part of header.i; comments stripped) is exactly an accepted domain.
    ARC-Authentication-Results are never read (a sender can mint them).

    Order: (1) a trusted AR in AgentMail's parsed headers decides. (2) Otherwise,
    when the caller injected `raw_fetch` (returns the raw RFC-822 message), the
    decision runs over the raw message's first trusted AR (raw_auth_headers),
    tagged source="raw-fallback" — BEFORE any signature-presence check, so an
    unverified DKIM-Signature never qualifies a payment while raw is available. No
    trusted AR in the raw message is fail(no-receiver-ar); a fetch that fails twice
    is fail(fetch-failed) — both land in the near-miss ping for human eyes. (3) Only
    with no raw_fetch at all (legacy callers/tests): a DKIM-Signature carrying
    d=<accepted domain> + AgentMail's authed bit is pass(medium); nothing at all is
    unknown(unavailable), which evaluate() never qualifies.
    """
    h = _ci_headers(headers)
    labels = [str(x).lower() for x in (labels or [])]
    authed = "unauthenticated" not in labels and event_type != "message.received.unauthenticated"

    ar = " ".join((h.get("authentication-results") or "").split())
    if ar and _authserv_id(ar) in _TRUSTED_AUTHSERV_IDS:
        return _dkim_from_ar(ar, authed)
    if raw_fetch is None:
        return _dkim_from_signature(h, authed)

    raw, why = _fetch_raw(raw_fetch)
    if raw is None:
        return DkimResult("fail", "fetch-failed", authed,
                          f"raw-fallback fetch failed twice ({why}); parsed headers carry "
                          f"no trusted Authentication-Results, so DKIM is unverified",
                          source="raw-fallback")

    rh = _ci_headers(raw_auth_headers(raw))
    if not rh:
        # Real Mercury mail always carries SES's stamp. No receiver-anchored AR
        # means the raw message can't vouch for itself: fail, so a payment-shaped
        # email lands in the near-miss ping for human eyes.
        return DkimResult("fail", "no-receiver-ar", authed,
                          f"raw-fallback: no Authentication-Results from a known receiver "
                          f"({' / '.join(_TRUSTED_AUTHSERV_IDS)}) in the raw message",
                          source="raw-fallback")
    res = _dkim_from_ar(rh["authentication-results"], authed)
    res.source = "raw-fallback"
    res.detail = f"raw-fallback: {res.detail}"
    return res


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
    raw_fetch: RawFetcher | None = None,
) -> Decision:
    """Combine the three gate layers into a qualify/ignore decision.

    `raw_fetch` (optional) is handed to dkim_check for the raw-message fallback.
    It is only offered for payment-SHAPED mail (recipient or subject matched), so
    plain spam on the inbox never costs a raw download.
    """
    reasons: list[str] = []
    code = extract_finance_code(recipients, headers)
    subj = parse_payment_subject(subject)
    shaped = code is not None or subj is not None
    dk = dkim_check(headers, labels, event_type, raw_fetch=raw_fetch if shaped else None)

    if code is None:
        reasons.append("recipient does not match finance+<code>@staygoldenhi.com")
    if subj is None:
        reasons.append('subject does not match "<payor> sent you $X"')

    if dk.verdict == "pass":
        dkim_ok = True
    elif dk.verdict == "unknown":
        # No auth headers anywhere. AgentMail's generic authed bit is NOT enough to
        # qualify a payment (review r2 X3) — a payment-shaped one goes to the
        # near-miss ping instead.
        dkim_ok = False
        reasons.append(f"DKIM unverifiable ({dk.detail})")
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
        f"DKIM gate: {decision.dkim.verdict} ({decision.dkim.strength}, "
        f"via {decision.dkim.source}) — {decision.dkim.detail}\n\n"
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

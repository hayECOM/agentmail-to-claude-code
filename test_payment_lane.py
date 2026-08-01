"""Tests for payment_lane.py — the cortana.h qualify-and-spawn gate.

The pure gate functions are tested directly; the Cortana spawn is tested against
a fake spawn-coder.sh (test-coder.sh fake-binary style) injected via the module's
SPAWN_CODER / PROMPT_ROOT attributes.
"""

from __future__ import annotations

import os
import pathlib
import stat
import tempfile

import payment_lane as pl


# --- recipient gate ----------------------------------------------------------

def test_extract_code_from_recipient_list():
    assert pl.extract_finance_code(["finance+po1183@staygoldenhi.com"], None) == "po1183"


def test_extract_code_case_insensitive_and_display_name():
    to = ["Cortana <Finance+ABC-9@StayGoldenHI.com>"]
    assert pl.extract_finance_code(to, None) == "ABC-9"


def test_extract_code_from_delivered_to_header_when_to_rewritten():
    # Gmail auto-forward: envelope To is cortana.h, true recipient in Delivered-To.
    to = ["cortana.h@agentmail.to"]
    headers = {"Delivered-To": "finance+wire42@staygoldenhi.com"}
    assert pl.extract_finance_code(to, headers) == "wire42"


def test_extract_code_none_for_other_domain():
    assert pl.extract_finance_code(["finance+x@example.com"], None) is None


def test_extract_code_none_for_plain_finance():
    assert pl.extract_finance_code(["finance@staygoldenhi.com"], None) is None


# --- lane-2 direct-send recipient shape (derived from CC_LANE2_INBOX) ---------
# Mercury sends straight to the AgentMail inbox as cortana.h+<code>@agentmail.to
# (plus-addressing delivery verified live 2026-07-05, To header survives intact).
# The accepted base is derived from CC_LANE2_INBOX, mirrored in pl.LANE2_INBOX.

_LANE2_INBOX = "cortana.h@agentmail.to"


def test_extract_code_from_lane2_plus_address_direct_send(monkeypatch):
    monkeypatch.setattr(pl, "LANE2_INBOX", _LANE2_INBOX)
    assert pl.extract_finance_code(["cortana.h+po1183@agentmail.to"], None) == "po1183"


def test_extract_code_lane2_case_insensitive_and_display_name(monkeypatch):
    monkeypatch.setattr(pl, "LANE2_INBOX", _LANE2_INBOX)
    to = ["Cortana <Cortana.H+ABC-9@AgentMail.to>"]
    assert pl.extract_finance_code(to, None) == "ABC-9"


def test_extract_code_lane2_from_to_header_direct_send(monkeypatch):
    # Direct send: envelope To survives intact, no Delivered-To rewrite involved.
    monkeypatch.setattr(pl, "LANE2_INBOX", _LANE2_INBOX)
    headers = {"To": "cortana.h+wire42@agentmail.to"}
    assert pl.extract_finance_code(["cortana.h@agentmail.to"], headers) == "wire42"


def test_extract_code_lane2_bare_inbox_is_not_a_code(monkeypatch):
    # The plain inbox (no +<code>) is not a qualifying finance recipient.
    monkeypatch.setattr(pl, "LANE2_INBOX", _LANE2_INBOX)
    assert pl.extract_finance_code(["cortana.h@agentmail.to"], None) is None


def test_extract_code_lane2_lookalikes_rejected(monkeypatch):
    # Boundary-anchored per the PR-8 posture: a bare substring must not wave through
    # an address that only LOOKS like the accepted plus-address.
    monkeypatch.setattr(pl, "LANE2_INBOX", _LANE2_INBOX)
    for bad in (
        "cortana.h.evil+cle@agentmail.to",       # extra label between local part and '+'
        "notcortana.h+cle@agentmail.to",         # prefix glued onto the local part
        "cortana.h+cle@agentmail.to.evil.com",   # domain-suffix look-alike
        "cortana.h+cle@evilagentmail.to",        # registrable look-alike domain
    ):
        assert pl.extract_finance_code([bad], None) is None, f"{bad} must not match"


def test_extract_code_lane2_disabled_when_inbox_unset(monkeypatch):
    # No CC_LANE2_INBOX -> the derived shape is off; only finance+ qualifies.
    monkeypatch.setattr(pl, "LANE2_INBOX", "")
    assert pl.extract_finance_code(["cortana.h+po7@agentmail.to"], None) is None


def test_finance_alias_still_qualifies_when_lane2_enabled(monkeypatch):
    # The two shapes coexist: enabling the lane-2 base doesn't drop the SG alias.
    monkeypatch.setattr(pl, "LANE2_INBOX", _LANE2_INBOX)
    assert pl.extract_finance_code(["finance+po1183@staygoldenhi.com"], None) == "po1183"


# --- subject gate ------------------------------------------------------------

def test_subject_parse_basic():
    assert pl.parse_payment_subject("Acme Corp sent you $4,200.00") == ("Acme Corp", "4200.00")


def test_subject_parse_no_cents_no_dollar():
    assert pl.parse_payment_subject("Bob sent you 50") == ("Bob", "50")


def test_subject_parse_rejects_unrelated():
    assert pl.parse_payment_subject("Re: your invoice") is None
    assert pl.parse_payment_subject("") is None
    assert pl.parse_payment_subject(None) is None


def test_reply_or_forward_prefix_is_not_a_payment_subject():
    # Live 2026-07-04 16:46: a Gmail REPLY on a probe thread ("Re: Golden Hour
    # Studios Inc. sent you $0.04") parsed as a payment (payor mis-parsed as
    # "Re: Golden Hour...") and cost a session spawn. Auto-forwarded Mercury mail
    # keeps the original subject verbatim — re:/fwd: only marks a human reply or
    # manual forward, so it must NOT parse as a payment.
    reply = "Re: Golden Hour Studios Inc. sent you $0.04"
    assert pl.parse_payment_subject(reply) is None
    assert pl.parse_payment_subject("Fwd: Acme Corp sent you $4,200.00") is None
    assert pl.parse_payment_subject("fwd: Bob sent you 50") is None
    # the un-prefixed original still parses fine
    assert pl.parse_payment_subject("Golden Hour Studios Inc. sent you $0.04") \
        == ("Golden Hour Studios Inc.", "0.04")
    # and end to end: the reply no longer qualifies the gate (no spawn), even with a
    # valid recipient + staygoldenhi DKIM pass (the exact 16:46 conditions).
    d = pl.evaluate(["finance+cle@staygoldenhi.com"], _GOOD_AR, reply, [], "message.received")
    assert d.qualified is False


# --- DKIM gate ---------------------------------------------------------------

def test_dkim_strong_pass_from_authentication_results():
    h = {"Authentication-Results":
         "mx.google.com; dkim=pass header.i=@staygoldenhi.com header.s=google; spf=pass"}
    r = pl.dkim_check(h, [], "message.received")
    assert r.verdict == "pass" and r.strength == "strong"


def test_dkim_medium_pass_from_signature_when_authed():
    h = {"DKIM-Signature": "v=1; a=rsa-sha256; d=staygoldenhi.com; s=google; h=from:to"}
    r = pl.dkim_check(h, [], "message.received")
    assert r.verdict == "pass" and r.strength == "medium"
    # Same acceptance for Mercury's own signing domain (d=mg.mercury.com, selector pic).
    h = {"DKIM-Signature": "v=1; a=rsa-sha256; d=mg.mercury.com; s=pic; h=from:to"}
    r = pl.dkim_check(h, [], "message.received")
    assert r.verdict == "pass" and r.strength == "medium"
    assert "mg.mercury.com" in r.detail


def test_dkim_signature_but_unauthenticated_is_fail():
    h = {"DKIM-Signature": "v=1; d=staygoldenhi.com; s=google"}
    r = pl.dkim_check(h, ["unauthenticated"], "message.received.unauthenticated")
    assert r.verdict == "fail"


def test_dkim_unknown_when_no_auth_headers():
    r = pl.dkim_check({}, [], "message.received")
    assert r.verdict == "unknown" and r.strength == "unavailable"
    assert r.authed is True  # AgentMail didn't flag it


def test_dkim_present_but_wrong_domain_is_fail():
    h = {"Authentication-Results": "mx.google.com; dkim=pass header.i=@evil.com; spf=fail"}
    r = pl.dkim_check(h, [], "message.received")
    assert r.verdict == "fail" and r.strength == "present-no-match"


def test_dkim_lookalike_domains_do_not_slip_past_the_boundary():
    # Accepting mercury.com must NOT accept a registrable look-alike (evilmercury.com)
    # or a same-prefix subdomain trick (mercury.company.com). The domain match is
    # boundary-anchored so a bare substring can't wave these through the payment gate.
    for bad_domain in ("evilmercury.com", "mercury.company.com", "notstaygoldenhi.com"):
        h = {"Authentication-Results": f"mx.google.com; dkim=pass header.i=@{bad_domain}"}
        r = pl.dkim_check(h, [], "message.received")
        assert r.verdict == "fail", f"{bad_domain} must not qualify"


# --- evaluate (the combined decision) ----------------------------------------

_GOOD_AR = {"Authentication-Results": "mx.google.com; dkim=pass header.i=@staygoldenhi.com"}


def test_evaluate_qualifies_all_three():
    d = pl.evaluate(["finance+po7@staygoldenhi.com"], _GOOD_AR,
                    "Acme sent you $1,000.00", [], "message.received")
    assert d.qualified is True
    assert d.code == "po7" and d.payor == "Acme" and d.amount == "1000.00"
    assert d.case_ref == "MERC-po7"


def test_evaluate_qualifies_with_forwarded_mercury_origin_dkim():
    # Real probe shape (2026-07-04): on a Gmail auto-forward the surviving DKIM pass
    # is Mercury's ORIGIN domain — dkim=pass header.i=@mg.mercury.com (selector pic) —
    # not the staygoldenhi.com re-sign. Qualifies when recipient + subject also pass.
    ar = {"Authentication-Results":
          "mx.google.com; dkim=pass header.i=@mg.mercury.com header.s=pic; spf=pass"}
    d = pl.evaluate(["finance+po7@staygoldenhi.com"], ar,
                    "Golden Hour Studios Inc. sent you $38,548.70", [], "message.received")
    assert d.qualified is True
    assert d.dkim.verdict == "pass" and d.dkim.strength == "strong"
    assert "mg.mercury.com" in d.dkim.detail


def test_evaluate_qualifies_lane2_direct_send_with_mercury_dkim(monkeypatch):
    # Direct-to-AgentMail send (2026-07-05): Mercury delivers straight to the inbox
    # as cortana.h+<code>@agentmail.to (To header intact, no Gmail auto-forward), and
    # DKIM is Mercury's own d=mg.mercury.com — accepted since PR #8. All three pass.
    monkeypatch.setattr(pl, "LANE2_INBOX", _LANE2_INBOX)
    ar = {"Authentication-Results":
          "mx.agentmail.to; dkim=pass header.i=@mg.mercury.com header.s=pic; spf=pass"}
    d = pl.evaluate(["cortana.h+po7@agentmail.to"], ar,
                    "Golden Hour Studios Inc. sent you $38,548.70", [], "message.received")
    assert d.qualified is True
    assert d.code == "po7" and d.case_ref == "MERC-po7"
    assert d.dkim.verdict == "pass" and d.dkim.strength == "strong"


def test_evaluate_rejects_lane2_lookalike_recipient(monkeypatch):
    # A look-alike direct-send recipient fails the recipient gate -> no qualify.
    monkeypatch.setattr(pl, "LANE2_INBOX", _LANE2_INBOX)
    ar = {"Authentication-Results":
          "mx.agentmail.to; dkim=pass header.i=@mg.mercury.com header.s=pic"}
    d = pl.evaluate(["notcortana.h+po7@agentmail.to"], ar,
                    "Acme sent you $10", [], "message.received")
    assert d.qualified is False and d.code is None


def test_evaluate_rejects_bad_recipient():
    d = pl.evaluate(["someoneelse@staygoldenhi.com"], _GOOD_AR,
                    "Acme sent you $10", [], "message.received")
    assert d.qualified is False and d.code is None


def test_evaluate_rejects_bad_subject():
    d = pl.evaluate(["finance+po7@staygoldenhi.com"], _GOOD_AR,
                    "hello there", [], "message.received")
    assert d.qualified is False


def test_evaluate_rejects_dkim_fail_even_if_recipient_subject_ok():
    bad = {"Authentication-Results": "mx; dkim=pass header.i=@evil.com"}
    d = pl.evaluate(["finance+po7@staygoldenhi.com"], bad,
                    "Acme sent you $10", [], "message.received")
    assert d.qualified is False


def test_evaluate_unknown_dkim_but_authed_qualifies_on_the_rest():
    d = pl.evaluate(["finance+po7@staygoldenhi.com"], {},
                    "Acme sent you $10", [], "message.received")
    assert d.qualified is True
    assert any("DKIM headers unavailable" in r for r in d.reasons)


def test_evaluate_unknown_dkim_and_unauthenticated_is_rejected():
    d = pl.evaluate(["finance+po7@staygoldenhi.com"], {},
                    "Acme sent you $10", ["unauthenticated"],
                    "message.received.unauthenticated")
    assert d.qualified is False


# --- prompt + spawn ----------------------------------------------------------

def _decision():
    return pl.evaluate(["finance+po7@staygoldenhi.com"], _GOOD_AR,
                       "Acme sent you $1,000.00", [], "message.received")


def test_build_prompt_has_playbook_caseref_and_tg_guidance():
    d = _decision()
    p = pl.build_cortana_prompt(d, "Mercury <no-reply@mercury.com>",
                                "Acme sent you $1,000.00", "You received a payment.",
                                thread_id="th_1", inbox="cortana.h@agentmail.to")
    assert "logging mercury GPO payment" in p
    assert "MERC-po7" in p
    assert "--case-ref MERC-po7" in p
    assert "get_thread" in p            # thread context block present
    assert "You are Cortana" in p


# --- near-miss ping ----------------------------------------------------------

def _fake_tg_send(prefix):
    """A fake tg-send that logs each argv on its own line and exits 0.

    Returns (path, argv_log). Injected via monkeypatch on pl.TG_SEND, mirroring the
    fake-spawn-coder.sh style below.
    """
    tmp = pathlib.Path(tempfile.mkdtemp(prefix=prefix))
    argv_log = tmp / "tg-argv.log"
    fake = tmp / "fake-tg-send.sh"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" >> "{argv_log}"\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake, argv_log


def test_is_payment_shaped_recipient_or_subject():
    # subject-only match (bad recipient) is still payment-shaped
    d = pl.evaluate(["nope@example.com"], _GOOD_AR, "Acme sent you $10", [], "message.received")
    assert d.qualified is False and pl.is_payment_shaped(d) is True
    # recipient-only match (bad subject) is still payment-shaped
    d = pl.evaluate(["finance+po7@staygoldenhi.com"], _GOOD_AR, "hello", [], "message.received")
    assert d.qualified is False and pl.is_payment_shaped(d) is True
    # neither -> not payment-shaped
    d = pl.evaluate(["nope@example.com"], {}, "win a prize", [], "message.received")
    assert pl.is_payment_shaped(d) is False


def test_ping_on_near_miss_dkim_fail(monkeypatch):
    fake, argv_log = _fake_tg_send("pl-ping-")
    monkeypatch.setattr(pl, "TG_SEND", str(fake))

    # Auto-forwarded mail: recipient + subject match, but the surviving DKIM pass is
    # an unrecognized origin domain — here the sender's personal gmail signing domain
    # (per the 2026-07-04 probe), not SG's re-sign or Mercury's mg.mercury.com.
    bad = {"Authentication-Results":
           "mx.google.com; dkim=pass header.i=@ronyhay-com.20251104.gappssmtp.com"}
    d = pl.evaluate(["finance+po7@staygoldenhi.com"], bad,
                    "Acme sent you $4,200.00", [], "message.received")
    assert d.qualified is False

    assert pl.ping_near_miss(d, "Acme sent you $4,200.00") is True
    argv = argv_log.read_text()
    assert "--topic" in argv and "cortana" in argv   # cortana topic, not stay_golden
    assert "stay_golden" not in argv
    assert "Acme sent you $4,200.00" in argv         # subject surfaced
    assert "DKIM check failed" in argv               # decision.reasons surfaced


def test_ping_on_near_miss_recipient_match_bad_subject(monkeypatch):
    fake, argv_log = _fake_tg_send("pl-ping-recip-")
    monkeypatch.setattr(pl, "TG_SEND", str(fake))

    # finance+<code> recipient matches (real PO anchor) but the subject didn't parse.
    d = pl.evaluate(["finance+po7@staygoldenhi.com"], _GOOD_AR,
                    "Payment notification", [], "message.received")
    assert d.qualified is False
    assert pl.ping_near_miss(d, "Payment notification") is True
    argv = argv_log.read_text()
    assert "cortana" in argv
    assert 'subject does not match' in argv          # decision.reasons surfaced


def test_ping_on_near_miss_lane2_recipient_bad_subject(monkeypatch):
    fake, argv_log = _fake_tg_send("pl-ping-lane2-")
    monkeypatch.setattr(pl, "TG_SEND", str(fake))
    monkeypatch.setattr(pl, "LANE2_INBOX", _LANE2_INBOX)

    # Direct-send recipient matches (cortana.h+<code>) but the subject didn't parse:
    # payment-shaped near-miss, recognized by the new plus-address shape.
    d = pl.evaluate(["cortana.h+po7@agentmail.to"], _GOOD_AR,
                    "Payment notification", [], "message.received")
    assert d.qualified is False
    assert pl.is_payment_shaped(d) is True
    assert pl.ping_near_miss(d, "Payment notification") is True
    argv = argv_log.read_text()
    assert "cortana" in argv
    assert "stay_golden" not in argv


def test_lane2_lookalike_recipient_is_not_payment_shaped(monkeypatch):
    # A look-alike recipient with a non-payment subject matches NEITHER gate, so it's
    # an unsolicited probe with no oracle -> silent, no near-miss ping.
    fake, argv_log = _fake_tg_send("pl-lookalike-")
    monkeypatch.setattr(pl, "TG_SEND", str(fake))
    monkeypatch.setattr(pl, "LANE2_INBOX", _LANE2_INBOX)

    d = pl.evaluate(["notcortana.h+po7@agentmail.to"], {}, "hello there",
                    [], "message.received")
    assert pl.is_payment_shaped(d) is False
    assert pl.ping_near_miss(d, "hello there") is False
    assert not argv_log.exists()


def test_silent_on_spam_non_payment_shaped(monkeypatch):
    fake, argv_log = _fake_tg_send("pl-silent-")
    monkeypatch.setattr(pl, "TG_SEND", str(fake))

    # Neither recipient nor subject matches -> a probe, no oracle, no ping.
    d = pl.evaluate(["random@example.com"], {}, "You won a prize!!!",
                    [], "message.received")
    assert d.qualified is False
    assert pl.ping_near_miss(d, "You won a prize!!!") is False
    assert not argv_log.exists()                     # tg-send never invoked


def test_spawn_cortana_invokes_spawn_coder_and_returns_handle(monkeypatch):
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="pl-spawn-"))
    argv_log = tmp / "argv.log"
    fake = tmp / "fake-spawn-coder.sh"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{argv_log}"\n'
        'echo "workspace:42"\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    monkeypatch.setattr(pl, "SPAWN_CODER", str(fake))
    monkeypatch.setattr(pl, "PROMPT_ROOT", tmp / "prompts")
    monkeypatch.setattr(pl, "CORTANA_REPO", "/Users/hayecom/Cortana")

    d = _decision()
    handle = pl.spawn_cortana(d, "the brief body")
    assert handle == "workspace:42"

    argv = argv_log.read_text()
    assert "--repo /Users/hayecom/Cortana" in argv
    assert "--title Cortana: mercury MERC-po7" in argv
    assert "--group Cortana" in argv
    # the brief points at the written prompt file
    prompt_file = tmp / "prompts" / "MERC-po7.md"
    assert prompt_file.exists()
    assert "the brief body" in prompt_file.read_text()
    assert str(prompt_file) in argv

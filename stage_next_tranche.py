#!/usr/bin/env python3
"""Stage the next tranche invoice after a split-terms tranche is paid.

Run by the lane-2 payment session (the `logging mercury GPO payment` playbook)
right after it commits a payment, once per invoice the wire paid in full:

    stage_next_tranche.py --base prod --invoice recZXameKHqNujg4N

When that invoice sits on two-part split terms (`30/70`) and the PO has no
invoice for the next tranche yet, this creates it at `Payment Status: Estimate`
(same Linked PO and terms, seq + 1, amount forced by the ratio to the cent, the
paid tranche's PDF), and APPENDS a dated note line to the PO. The balance wire
then reconciles hands-free; if the real balance drifts, the lane's to-the-cent
gate escalates. That failure direction is the designed one (INV#78 / PAY#47).

It creates NOTHING unless the arithmetic is forced. Terms that aren't a clean
two-part ratio, a ratio that doesn't land on whole cents, or any other invoice
on the PO (a separately-invoiced on-receipt item moves the split base onto the
open balance — playbook lesson 2026-08-07) produce a FLAG line for the session
to escalate. A guessed estimate is worse than none.

Idempotent: a sibling invoice at seq + 1 in any status means NOOP.

Output is one line, `TRANCHE <STAGED|NOOP|SKIP|FLAG> ...`. Exit 0 for
STAGED/NOOP/SKIP, 3 for FLAG (escalate), 1 for an error (nothing or a partial
write happened; the line says which). Stdlib only, so any python3 runs it.
PAT + REST per the house posture, never the Airtable MCP.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

ENV_FILE = pathlib.Path(
    os.environ.get("PO_TRACKER_ENV", pathlib.Path.home() / "Workspace" / "tools" / "po-payment-flow" / ".env")
)
NOTE_TZ = ZoneInfo(os.environ.get("PO_TRACKER_TZ", "America/Los_Angeles"))
API = "https://api.airtable.com/v0"
INVOICES = "tbljxLxn2XnwF5gT8"
PURCHASE_ORDERS = "tblIrbt8qjhEuhKpv"

_SPLIT_RE = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")


@dataclass
class Plan:
    action: str                       # "stage" | "noop" | "skip" | "flag"
    reason: str
    fields: dict = field(default_factory=dict)   # the new invoice, when staging
    amount_cents: int = 0
    pct: int = 0


def _cents(value) -> int | None:
    """Exact cents of a currency value, or None if it isn't whole cents."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None
    c = d * 100
    if c != c.to_integral_value():
        return None
    return int(c)


def fmt_usd(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


def _label(inv: dict) -> str:
    f = inv.get("fields", {})
    return f.get("Invoice") or f"INV#{f.get('Invoice ID', '?')}"


def _short(inv: dict) -> str:
    """INV#NN — what the tracker notes use (and no `_` for rich text to escape)."""
    f = inv.get("fields", {})
    return f"INV#{f['Invoice ID']}" if f.get("Invoice ID") else _label(inv)


def plan_next_tranche(paid: dict, po_invoices: list[dict], po_name: str, note_date: str) -> Plan:
    """Decide what to do for one paid invoice. Pure: records in, Plan out.

    `paid` and each of `po_invoices` are Airtable records ({"id", "fields"});
    `po_invoices` is every invoice linked to the parent PO, `paid` included.
    """
    f = paid.get("fields", {})
    terms = (f.get("Payment Terms") or "").strip()
    if "/" not in terms:
        return Plan("skip", f"terms {terms or '(none)'!r} are not split")
    m = _SPLIT_RE.match(terms)
    if not m:
        return Plan("flag", f"terms {terms!r} are not a clean two-part ratio")
    first, second = int(m.group(1)), int(m.group(2))
    if first <= 0 or second <= 0 or first + second != 100:
        return Plan("flag", f"terms {terms!r} don't split 100%")

    seq = f.get("Invoice Seq")
    if seq == 2:
        return Plan("skip", f"seq 2 is the final tranche of {terms}")
    if seq != 1:
        return Plan("flag", f"Invoice Seq {seq!r} doesn't fit two-part terms {terms}")

    if f.get("Payment Status") != "Paid" or _cents(f.get("Remainder", 0)) != 0:
        return Plan("skip", f"not fully paid (status={f.get('Payment Status')!r}, "
                            f"remainder={f.get('Remainder')!r})")

    others = [i for i in po_invoices if i.get("id") != paid.get("id")]
    nxt = [i for i in others if i.get("fields", {}).get("Invoice Seq") == 2]
    if nxt:
        n = nxt[0]
        return Plan("noop", f"{_label(n)} ({n['id']}) already holds seq 2 "
                            f"[{n.get('fields', {}).get('Payment Status')}]")
    if others:
        listed = ", ".join(
            f"{_label(i)} seq={i.get('fields', {}).get('Invoice Seq')!r} "
            f"terms={i.get('fields', {}).get('Payment Terms')!r}" for i in others
        )
        return Plan("flag", f"PO carries other invoices ({listed}); the split may run on "
                            f"the open balance, not this tranche — stage by hand")

    paid_cents = _cents(f.get("Invoice Amount"))
    if not paid_cents or paid_cents <= 0:
        return Plan("flag", f"paid tranche amount {f.get('Invoice Amount')!r} isn't a positive "
                            f"whole-cent value")
    if (paid_cents * second) % first:
        return Plan("flag", f"{fmt_usd(paid_cents)} × {second}/{first} doesn't land on whole cents")
    amount = paid_cents * second // first

    pdf = [{"url": a["url"], "filename": a.get("filename") or "invoice.pdf"}
           for a in (f.get("Invoice PDF") or []) if a.get("url")]
    note = (f"{note_date}: {second}% balance tranche of {terms} on {po_name} — Estimate staged "
            f"from {_short(paid)} ({fmt_usd(paid_cents)} × {second}/{first} = {fmt_usd(amount)}) "
            f"(Cortana)")
    fields = {
        "Linked PO": f.get("Linked PO"),
        "Invoice Seq": 2,
        "Payment Status": "Estimate",
        "Payment Terms": terms,
        "Invoice Amount": amount / 100,
        "Notes": note,
    }
    if pdf:
        fields["Invoice PDF"] = pdf
    return Plan("stage", f"{second}% balance of {terms}", fields=fields,
                amount_cents=amount, pct=second)


def append_note(existing: str | None, line: str) -> str:
    """Append-only: existing notes stay byte-for-byte, the new line goes last."""
    base = existing or ""
    if base and not base.endswith("\n"):
        base += "\n"
    return f"{base}{line}\n"


# --- Airtable REST -----------------------------------------------------------

class Airtable:
    def __init__(self, pat: str, base: str):
        self.pat, self.base = pat, base

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            f"{API}/{self.base}/{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.pat}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Airtable {method} {path}: {e.code} {e.read()[:300]!r}") from e

    def get(self, table: str, rec: str) -> dict:
        return self._call("GET", f"{table}/{rec}")

    def create(self, table: str, fields: dict) -> dict:
        return self._call("POST", table, {"fields": fields})

    def patch(self, table: str, rec: str, fields: dict) -> dict:
        return self._call("PATCH", f"{table}/{rec}", {"fields": fields})


def run(at: Airtable, invoice_id: str, note_date: str, dry_run: bool) -> tuple[int, str]:
    paid = at.get(INVOICES, invoice_id)
    links = paid.get("fields", {}).get("Linked PO") or []
    if len(links) != 1:
        return 3, f"TRANCHE FLAG {_label(paid)}: Linked PO is {links!r}, expected exactly one"
    po = at.get(PURCHASE_ORDERS, links[0])
    po_name = po.get("fields", {}).get("PO ID v2") or links[0]
    po_invoices = [paid if i == paid["id"] else at.get(INVOICES, i)
                   for i in po.get("fields", {}).get("Invoice") or []]
    if not any(i["id"] == paid["id"] for i in po_invoices):
        po_invoices.append(paid)

    plan = plan_next_tranche(paid, po_invoices, po_name, note_date)
    head = f"{_label(paid)} on {po_name}"
    if plan.action == "flag":
        return 3, f"TRANCHE FLAG {head}: {plan.reason}"
    if plan.action in ("noop", "skip"):
        return 0, f"TRANCHE {plan.action.upper()} {head}: {plan.reason}"

    what = f"seq 2 Estimate {fmt_usd(plan.amount_cents)} ({plan.reason})"
    if dry_run:
        shown = {**plan.fields, "Invoice PDF": [a["filename"] for a in plan.fields.get("Invoice PDF", [])]}
        return 0, f"TRANCHE DRY-RUN {head}: would stage {what}; fields={json.dumps(shown, ensure_ascii=False)}"

    created = at.create(INVOICES, plan.fields)
    new_label = f"INV#{created.get('fields', {}).get('Invoice ID', '?')}"
    po_line = (f"{note_date}: {new_label} staged at Estimate ({fmt_usd(plan.amount_cents)}, "
               f"{plan.pct}% balance per {plan.fields['Payment Terms']} terms on "
               f"{_short(paid)}) (Cortana)")
    try:
        fresh = at.get(PURCHASE_ORDERS, po["id"])  # re-read right before the append
        at.patch(PURCHASE_ORDERS, po["id"],
                 {"Notes": append_note(fresh.get("fields", {}).get("Notes"), po_line)})
    except Exception as e:
        return 1, (f"TRANCHE PARTIAL {head}: staged {new_label} ({created['id']}) but the PO "
                   f"note append failed: {e}")
    pdf = "PDF copied" if "Invoice PDF" in plan.fields else "NO PDF on the paid tranche"
    return 0, f"TRANCHE STAGED {head}: {new_label} ({created['id']}) {what}, {pdf}"


def _load_env(path: pathlib.Path) -> dict:
    out = {}
    if path.exists():
        for ln in path.read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                out[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--invoice", required=True, help="record id of the invoice the wire paid")
    ap.add_argument("--base", required=True, choices=("prod", "sandbox"))
    ap.add_argument("--dry-run", action="store_true", help="decide and print, write nothing")
    ap.add_argument("--date", help="note date MM/DD/YYYY (default: today, America/Los_Angeles)")
    a = ap.parse_args(argv)

    env = {**_load_env(ENV_FILE), **{k: v for k, v in os.environ.items() if k.startswith("AIRTABLE_")}}
    pat = env.get("AIRTABLE_PAT")
    base = env.get("AIRTABLE_BASE_PROD" if a.base == "prod" else "AIRTABLE_BASE_SANDBOX")
    if not pat or not base:
        print(f"TRANCHE ERROR: AIRTABLE_PAT / base id for {a.base} missing ({ENV_FILE})")
        return 1
    note_date = a.date or dt.datetime.now(NOTE_TZ).strftime("%m/%d/%Y")
    try:
        code, line = run(Airtable(pat, base), a.invoice, note_date, a.dry_run)
    except Exception as e:
        code, line = 1, f"TRANCHE ERROR {a.invoice}: {e}"
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())

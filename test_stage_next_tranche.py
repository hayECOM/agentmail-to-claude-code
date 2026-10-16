"""Tests for stage_next_tranche.py — next-tranche Estimate staging.

The golden case is GPO#47-CLE on 2026-10-16: INV#89 (recZXameKHqNujg4N, the 30%
deposit, $17,185.68, paid by PAY#53) and INV#92 (recNeI6lqUFTe44Lw, the 70%
balance Estimate, $40,099.92) that Cortana staged by hand. Airtable is faked;
nothing here touches a real base.
"""

from __future__ import annotations

import copy
import threading
import time

import pytest

import stage_next_tranche as st

PO_ID = "reca3picNO7R1x1rQ"
PDF = [{"id": "att1", "url": "https://dl.airtable.com/pi.pdf", "filename": "PI of GPO47 2026.9.2.pdf"}]

INV89 = {
    "id": "recZXameKHqNujg4N",
    "fields": {
        "Invoice": "INV#89_GPO#47-CLE-1", "Invoice ID": 89, "Invoice Seq": 1,
        "Linked PO": [PO_ID], "Payment Terms": "30/70", "Invoice Amount": 17185.68,
        "Remainder": 0, "Total Paid": 17185.68, "Payment Status": "Paid", "Invoice PDF": PDF,
    },
}
INV92 = {
    "id": "recNeI6lqUFTe44Lw",
    "fields": {
        "Invoice": "INV#92_GPO#47-CLE-2", "Invoice ID": 92, "Invoice Seq": 2,
        "Linked PO": [PO_ID], "Payment Terms": "30/70", "Invoice Amount": 40099.92,
        "Remainder": 40099.92, "Payment Status": "Estimate", "Invoice PDF": PDF,
    },
}
PO_NOTES = "10/16/2026: Payment $17,185.68 (PAY#53, 10/16/2026 wire) applied (Cortana)\n"


def _po(invoice_ids):
    return {"id": PO_ID, "fields": {"PO ID v2": "GPO#47-CLE", "Invoice": invoice_ids,
                                    "Notes": PO_NOTES}}


class FakeAirtable:
    def __init__(self, records):
        self.records = {r["id"]: copy.deepcopy(r) for r in records}
        self.created, self.patched = [], []

    def get(self, table, rec):
        return copy.deepcopy(self.records[rec])

    def create(self, table, fields):
        self.created.append((table, fields))
        rec = {"id": "recNEW", "fields": {**fields, "Invoice ID": 93}}
        self.records["recNEW"] = rec
        return rec

    def patch(self, table, rec, fields):
        self.patched.append((table, rec, fields))
        self.records[rec]["fields"].update(fields)
        return self.records[rec]


def _plan(paid, siblings):
    return st.plan_next_tranche(paid, [paid, *siblings], "GPO#47-CLE", "10/16/2026")


def _with(rec, **fields):
    r = copy.deepcopy(rec)
    r["fields"].update(fields)
    return r


# --- the golden example -------------------------------------------------------

def test_golden_pay53_is_a_noop_now_that_inv92_exists():
    at = FakeAirtable([INV89, INV92, _po([INV89["id"], INV92["id"]])])
    code, line = st.run(at, INV89["id"], "10/16/2026", dry_run=False)
    assert code == 0 and line.startswith("TRANCHE NOOP")
    assert "INV#92_GPO#47-CLE-2" in line
    assert at.created == [] and at.patched == []


def test_golden_math_reproduces_inv92_when_it_is_missing():
    at = FakeAirtable([INV89, _po([INV89["id"]])])
    code, line = st.run(at, INV89["id"], "10/16/2026", dry_run=False)
    assert code == 0 and line.startswith("TRANCHE STAGED"), line
    (table, fields), = at.created
    assert table == st.INVOICES
    assert fields["Invoice Amount"] == 40099.92
    assert fields["Invoice Seq"] == 2 and fields["Payment Status"] == "Estimate"
    assert fields["Payment Terms"] == "30/70" and fields["Linked PO"] == [PO_ID]
    assert fields["Invoice PDF"] == [{"url": PDF[0]["url"], "filename": PDF[0]["filename"]}]
    assert fields["Notes"].startswith("10/16/2026: ") and fields["Notes"].endswith("(Cortana)")
    assert "from INV#89 ($17,185.68 × 70/30 = $40,099.92)" in fields["Notes"]
    for computed in ("Invoice ID", "Invoice", "Invoice v1", "Remainder", "Total Paid"):
        assert computed not in fields
    # PO note: appended, never rewritten.
    (_, rec, po_fields), = at.patched
    assert rec == PO_ID
    assert po_fields["Notes"].startswith(PO_NOTES)
    tail = po_fields["Notes"][len(PO_NOTES):]
    assert tail == ("10/16/2026: INV#93 staged at Estimate ($40,099.92, 70% balance per 30/70 "
                    "terms on INV#89) (Cortana)\n")


def test_second_run_after_staging_is_a_noop():
    at = FakeAirtable([INV89, _po([INV89["id"]])])
    st.run(at, INV89["id"], "10/16/2026", dry_run=False)
    at.records[PO_ID]["fields"]["Invoice"] = [INV89["id"], "recNEW"]
    at.records["recNEW"]["fields"]["Invoice Seq"] = 2
    code, line = st.run(at, INV89["id"], "10/16/2026", dry_run=False)
    assert code == 0 and line.startswith("TRANCHE NOOP")
    assert len(at.created) == 1


def test_dry_run_writes_nothing():
    at = FakeAirtable([INV89, _po([INV89["id"]])])
    code, line = st.run(at, INV89["id"], "10/16/2026", dry_run=True)
    assert code == 0 and line.startswith("TRANCHE DRY-RUN") and "$40,099.92" in line
    assert PDF[0]["url"] not in line and PDF[0]["filename"] in line
    assert at.created == [] and at.patched == []


def test_sibling_in_any_status_blocks_staging():
    for status in ("Estimate", "Unpaid", "Partially Paid", "Paid"):
        p = _plan(INV89, [_with(INV92, **{"Payment Status": status})])
        assert p.action == "noop", status


# --- create nothing when the math isn't forced -----------------------------------

def test_three_part_terms_flag():
    p = _plan(_with(INV89, **{"Payment Terms": "30/65/5"}), [])
    assert p.action == "flag" and "two-part" in p.reason


def test_ratio_not_on_whole_cents_flags():
    # $100.01 × 70/30 = $233.356… — not a forced number.
    p = _plan(_with(INV89, **{"Invoice Amount": 100.01}), [])
    assert p.action == "flag" and "whole cents" in p.reason


def test_other_invoice_on_po_changes_the_split_base_and_flags():
    # GPO#42-CLE shape (playbook 2026-08-07): a separately invoiced on-receipt
    # molding fee means the 30/70 split ran on the open balance.
    mold = {"id": "recMOLD", "fields": {"Invoice": "INV#68", "Invoice Seq": 1,
                                        "Payment Terms": "On receipt", "Invoice Amount": 519.0,
                                        "Payment Status": "Paid", "Linked PO": [PO_ID]}}
    p = _plan(INV89, [mold])
    assert p.action == "flag" and "open balance" in p.reason
    at = FakeAirtable([INV89, mold, _po([INV89["id"], "recMOLD"])])
    code, line = st.run(at, INV89["id"], "10/16/2026", dry_run=False)
    assert code == 3 and line.startswith("TRANCHE FLAG") and at.created == []


def test_terms_not_summing_to_100_flag():
    assert _plan(_with(INV89, **{"Payment Terms": "30/60"}), []).action == "flag"


def test_odd_seq_flags():
    assert _plan(_with(INV89, **{"Invoice Seq": None}), []).action == "flag"
    assert _plan(_with(INV89, **{"Invoice Seq": 3}), []).action == "flag"


def test_linked_po_must_be_exactly_one():
    paid = _with(INV89, **{"Linked PO": []})
    code, line = st.run(FakeAirtable([paid]), INV89["id"], "10/16/2026", dry_run=False)
    assert code == 3 and "Linked PO" in line


# --- nothing to do ----------------------------------------------------------------

def test_non_split_terms_skip():
    for terms in ("On receipt", "Net 30", "", None):
        assert _plan(_with(INV89, **{"Payment Terms": terms}), []).action == "skip", terms


def test_final_tranche_paid_skips():
    p = _plan(_with(INV92, **{"Payment Status": "Paid", "Remainder": 0}), [INV89])
    assert p.action == "skip" and "final" in p.reason


def test_partially_paid_deposit_skips():
    p = _plan(_with(INV89, **{"Payment Status": "Partially Paid", "Remainder": 85.68}), [])
    assert p.action == "skip"


def test_staging_without_a_pdf_still_stages_and_says_so():
    at = FakeAirtable([_with(INV89, **{"Invoice PDF": None}), _po([INV89["id"]])])
    code, line = st.run(at, INV89["id"], "10/16/2026", dry_run=False)
    assert code == 0 and "NO PDF" in line
    assert "Invoice PDF" not in at.created[0][1]


def test_po_note_append_failure_reports_partial():
    class Flaky(FakeAirtable):
        def patch(self, *a, **k):
            raise RuntimeError("422")
    at = Flaky([INV89, _po([INV89["id"]])])
    code, line = st.run(at, INV89["id"], "10/16/2026", dry_run=False)
    assert code == 1 and line.startswith("TRANCHE PARTIAL") and "recNEW" in line


# --- helpers ----------------------------------------------------------------------

@pytest.mark.parametrize("existing,expected", [
    (None, "L\n"), ("", "L\n"), ("a\n", "a\nL\n"), ("a", "a\nL\n"),
])
def test_append_note_is_append_only(existing, expected):
    assert st.append_note(existing, "L") == expected


def test_other_ratios_compute_exactly():
    p = _plan(_with(INV89, **{"Payment Terms": "50/50", "Invoice Amount": 1234.56}), [])
    assert p.action == "stage" and p.amount_cents == 123456
    p = _plan(_with(INV89, **{"Payment Terms": "40/60", "Invoice Amount": 2000}), [])
    assert p.action == "stage" and p.amount_cents == 300000


# --- review r1 F3: read -> check -> create is serialized -------------------------

class LinkingAirtable(FakeAirtable):
    """create() also links the new invoice back onto its PO (as Airtable's reverse
    link does) and mints a fresh id; get() yields so racing threads interleave."""
    def __init__(self, records, barrier=None):
        super().__init__(records)
        self._lock = threading.Lock()
        self.barrier = barrier  # unlocked race: both runs pass the check before either creates

    def get(self, table, rec):
        time.sleep(0.01)
        with self._lock:
            return super().get(table, rec)

    def create(self, table, fields):
        if self.barrier:
            self.barrier.wait()
        with self._lock:
            n = len(self.created)
            rec_id, inv_id = f"recNEW{n}", 93 + n
            self.created.append((table, fields))
            self.records[rec_id] = {"id": rec_id, "fields": {**fields, "Invoice ID": inv_id}}
            self.records[PO_ID]["fields"]["Invoice"].append(rec_id)
            return copy.deepcopy(self.records[rec_id])


def _race(at, lock_path):
    out = []

    def one():
        if lock_path is None:
            out.append(st.run(at, INV89["id"], "10/16/2026", dry_run=False))
        else:
            with st.tranche_lock(lock_path, timeout=10):
                out.append(st.run(at, INV89["id"], "10/16/2026", dry_run=False))

    ts = [threading.Thread(target=one) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return sorted(line.split(" ")[1] for _, line in out)


def test_unlocked_race_duplicates_which_is_why_the_lock_exists():
    at = LinkingAirtable([INV89, _po([INV89["id"]])], barrier=threading.Barrier(2, timeout=5))
    _race(at, None)
    assert len(at.created) == 2  # the verified $40,099.92 duplicate


def test_locked_race_stages_once_and_the_second_run_noops(tmp_path):
    at = LinkingAirtable([INV89, _po([INV89["id"]])])
    assert _race(at, tmp_path / "tranche-stage.prod.lock") == ["NOOP", "STAGED"]
    assert len(at.created) == 1
    notes = at.records[PO_ID]["fields"]["Notes"]
    assert notes.count("staged at Estimate") == 1


def test_lock_timeout_raises_and_main_creates_nothing(tmp_path, monkeypatch, capsys):
    lock = tmp_path / "tranche-stage.prod.lock"
    monkeypatch.setattr(st, "LOCK_DIR", tmp_path)
    monkeypatch.setattr(st, "LOCK_TIMEOUT_S", 0.3)
    monkeypatch.setenv("AIRTABLE_PAT", "pat")
    monkeypatch.setenv("AIRTABLE_BASE_PROD", "appX")

    def must_not_run(*a, **k):
        raise AssertionError("ran without the lock")
    monkeypatch.setattr(st, "run", must_not_run)
    with st.tranche_lock(lock, timeout=1):
        with pytest.raises(TimeoutError):
            with st.tranche_lock(lock, timeout=0.3):
                pass
        assert st.main(["--base", "prod", "--invoice", INV89["id"]]) == 1
    assert capsys.readouterr().out.startswith("TRANCHE ERROR")


# --- review r1 F4: the PO note append survives an intervening writer ---------------

def test_note_written_between_check_and_patch_survives():
    other = "10/16/2026: Rony: balance due on arrival (manual)"

    class Intervening(FakeAirtable):
        def create(self, table, fields):
            # Another writer appends to the PO after our first GET, before our PATCH.
            self.records[PO_ID]["fields"]["Notes"] += other + "\n"
            return super().create(table, fields)

    at = Intervening([INV89, _po([INV89["id"]])])
    code, line = st.run(at, INV89["id"], "10/16/2026", dry_run=False)
    assert code == 0 and line.startswith("TRANCHE STAGED")
    notes = at.records[PO_ID]["fields"]["Notes"]
    assert other in notes and "INV#93 staged at Estimate" in notes
    assert notes.startswith(PO_NOTES)


# --- review r1 F5: a NOOP repairs the note a PARTIAL run left missing --------------

def test_partial_then_rerun_repairs_the_po_note_exactly_once():
    class FlakyOnce(FakeAirtable):
        fail = True

        def create(self, table, fields):
            rec = super().create(table, fields)
            self.records[PO_ID]["fields"]["Invoice"].append(rec["id"])
            return rec

        def patch(self, *a, **k):
            if self.fail:
                self.fail = False
                raise RuntimeError("422")
            return super().patch(*a, **k)

    at = FlakyOnce([INV89, _po([INV89["id"]])])
    code, line = st.run(at, INV89["id"], "10/16/2026", dry_run=False)
    assert code == 1 and line.startswith("TRANCHE PARTIAL")
    assert "staged at Estimate" not in at.records[PO_ID]["fields"]["Notes"]

    code, line = st.run(at, INV89["id"], "10/17/2026", dry_run=False)
    assert code == 0 and line.startswith("TRANCHE NOOP (note repaired)"), line
    notes = at.records[PO_ID]["fields"]["Notes"]
    assert notes == PO_NOTES + ("10/17/2026: INV#93 staged at Estimate ($40,099.92, 70% balance "
                                "per 30/70 terms on INV#89) (Cortana)\n")

    code, line = st.run(at, INV89["id"], "10/17/2026", dry_run=False)
    assert code == 0 and line.startswith("TRANCHE NOOP ") and "repaired" not in line
    assert at.records[PO_ID]["fields"]["Notes"].count("staged at Estimate") == 1
    assert len(at.created) == 1


def test_noop_leaves_hand_staged_estimate_notes_alone():
    # INV#92 was staged by hand (no helper invoice note): the NOOP must not write
    # a PO line claiming the helper staged it. (Same as the golden test, explicit.)
    at = FakeAirtable([INV89, INV92, _po([INV89["id"], INV92["id"]])])
    code, line = st.run(at, INV89["id"], "10/16/2026", dry_run=False)
    assert line.startswith("TRANCHE NOOP ") and at.patched == []


def test_noop_repair_skipped_on_dry_run():
    staged = _with(INV92, Notes="10/16/2026: 70% balance tranche of 30/70 on GPO#47-CLE — "
                                "Estimate staged from INV#89 ($17,185.68 × 70/30 = $40,099.92) "
                                "(Cortana)")
    at = FakeAirtable([INV89, staged, _po([INV89["id"], INV92["id"]])])
    code, line = st.run(at, INV89["id"], "10/16/2026", dry_run=True)
    assert line.startswith("TRANCHE NOOP ") and at.patched == []
    code, line = st.run(at, INV89["id"], "10/16/2026", dry_run=False)
    assert line.startswith("TRANCHE NOOP (note repaired)")
    assert "INV#92 staged at Estimate ($40,099.92" in at.records[PO_ID]["fields"]["Notes"]

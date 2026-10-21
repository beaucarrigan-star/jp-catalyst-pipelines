# Original end date, renewals allowed, and the two computed end dates (16 Oct 2026).
# Run: python -m pytest tests -q  (from contract-pipeline/)
import json
import os
import sys
from datetime import date

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "con_worker"))

from packet_extract import PacketDoc, extract_packet  # noqa: E402
from text_norm import add_term_months  # noqa: E402
from writeback import derive_term_dates, plan_contract_fields  # noqa: E402
import review_payload  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def _read(name):
    with open(os.path.join(FIX, name + ".txt"), encoding="utf-8") as f:
        return f.read()


def _monroe():
    return extract_packet([PacketDoc(name="Monroe County, OH EM (2024).pdf", text=_read("monroe_oh"))], "Customer",
                          contract_has_account=True, packet_name="Monroe County, OH EM (2024).pdf")


def _hope():
    text = _read("SYNTHETIC_hope_school").replace("\n", "\f")
    amend = _read("hope_school_amend").replace("\n", "\f")
    return extract_packet([PacketDoc(name="Hope School EM Contract.pdf", text=text, doc_type="Base agreement"),
                           PacketDoc(name="AMENDMENT No. 1 to Electronic Monitoring Contract _ Hope Learning Academy.pdf",
                                     text=amend)], "Customer", contract_has_account=True)


# ---------------------------------------------------------------- the date arithmetic
def test_add_term_months():
    d = date
    assert add_term_months(d(2027, 2, 28), 12) == d(2028, 2, 29)   # month-end stays month-end (leap year)
    assert add_term_months(d(2028, 2, 29), 12) == d(2029, 2, 28)
    assert add_term_months(d(2026, 4, 30), 1) == d(2026, 5, 31)    # 30 Apr is April's last day -> 31 May
    assert add_term_months(d(2026, 6, 30), 12) == d(2027, 6, 30)
    assert add_term_months(d(2026, 12, 31), 12) == d(2027, 12, 31)
    assert add_term_months(d(2026, 6, 15), 24) == d(2028, 6, 15)   # mid-month days do not move
    assert add_term_months(d(2026, 1, 30), 1) == d(2026, 2, 28)    # 30 Jan is not month-end: clamp
    assert add_term_months(d(2026, 11, 30), 36) == d(2029, 11, 30)


def test_derive_term_dates():
    def eff(**kw):
        return lambda f: kw.get(f)
    out, notes = derive_term_dates(eff(Original_End_Date="2026-09-30", Renewals_Allowed=2, Renewal_Option_Length_Mo=12))
    assert out["End_After_1_Renewal"][0] == "2027-09-30" and out["Final_End_Date"][0] == "2028-09-30" and not notes
    out, _ = derive_term_dates(eff(Original_End_Date="2026-12-31", Renewals_Allowed=1, Renewal_Option_Length_Mo=6))
    assert out["End_After_1_Renewal"][0] == out["Final_End_Date"][0] == "2027-06-30"
    out, _ = derive_term_dates(eff(Original_End_Date="2026-12-31", Renewals_Allowed=0, Renewal_Option_Length_Mo=12))
    assert out == {"Final_End_Date": ("2026-12-31", "no renewals allowed, so the final end is the original end")}
    out, notes = derive_term_dates(eff(Original_End_Date="2026-12-31", Renewals_Allowed=2))     # months missing
    assert out == {} and "Renewal_Option_Length_Mo is empty" in notes[0]
    out, notes = derive_term_dates(eff(Renewals_Allowed=2, Renewal_Option_Length_Mo=12))        # no original end
    assert out == {} and "Original_End_Date is empty" in notes[0]
    assert derive_term_dates(eff()) == ({}, [])                                                  # nothing known, nothing said


# ---------------------------------------------------------------- extraction on real and synthetic packets
def test_monroe_original_end_and_renewals():
    v = {k: f.value for k, f in _monroe().terms.items()}
    assert v["Original_End_Date"] == "2026-09-30" and v["Contract_End_Date"] == "2026-09-30"
    assert v["Renewals_Allowed"] == 2 and v["Renewal_Options_Left"] == 2 and v["Renewal_Option_Length_Mo"] == 12
    res = _monroe()
    fields, notes = plan_contract_fields({}, res.terms, res.pricing)
    assert fields["Original_End_Date"] == "2026-09-30" and fields["Renewals_Allowed"] == 2
    assert fields["End_After_1_Renewal"] == "2027-09-30" and fields["Final_End_Date"] == "2028-09-30"
    assert fields["Auto_Renews"] == "Yes"


def test_hope_school_matches_crm():
    res = _hope()
    fields, _ = plan_contract_fields({}, res.terms, res.pricing, [(d.doc_type, d.effective_date) for d in res.docs])
    assert fields["Contract_Start_Date"] == "2025-07-01" and fields["Original_End_Date"] == "2027-06-30"
    assert fields["Renewals_Allowed"] == 2 and fields["Renewal_Option_Length_Mo"] == 12
    assert fields["End_After_1_Renewal"] == "2028-06-30"
    assert fields["Final_End_Date"] == "2029-06-30"      # what the CRM holds for Hope School today


def test_amendment_never_sets_original_end():
    """A later amendment moves Contract_End_Date, not Original_End_Date."""
    base = _read("SYNTHETIC_hope_school").replace("\n", "\f")
    amend = ("AMENDMENT No. 1. This Amendment is entered into as of January 5, 2027 by and between JusticePoint, Inc. "
             "and Hope School. The term of the Agreement shall be extended and shall end on June 30, 2028.")
    res = extract_packet([PacketDoc(name="Hope School EM Contract.pdf", text=base, doc_type="Base agreement"),
                          PacketDoc(name="Hope School Amendment No. 1.pdf", text=amend, doc_type="Amendment")],
                         "Customer", contract_has_account=True)
    t = {k: f.value for k, f in res.terms.items()}
    assert t["Original_End_Date"] == "2027-06-30"
    assert t["Contract_End_Date"] == "2028-06-30"
    fields, notes = plan_contract_fields({}, res.terms, res.pricing)
    assert any("after the original end" in n for n in notes)   # tells the reviewer to check Extensions Left


# ---------------------------------------------------------------- write rules
def test_existing_final_end_is_not_overwritten():
    """Computed dates are Medium: they fill an empty field but never overwrite a value on the record."""
    res = _monroe()
    contract = {"Final_End_Date": "2028-10-31", "End_After_1_Renewal": None}
    fields, notes = plan_contract_fields(contract, res.terms, res.pricing)
    assert "Final_End_Date" not in fields and fields["End_After_1_Renewal"] == "2027-09-30"
    assert any(n.startswith("Differs, left as is: Final_End_Date is 2028-10-31") and "2028-09-30" in n for n in notes)


def test_verified_contract_is_not_written():
    res = _monroe()
    fields, notes = plan_contract_fields({"Ingest_Status": "Ingested"}, res.terms, res.pricing)
    assert not any(k in fields for k in ("Original_End_Date", "Renewals_Allowed", "End_After_1_Renewal", "Final_End_Date"))
    assert any("Verified contract, not written: Original_End_Date" in n for n in notes)


def test_reviewed_inputs_drive_the_dates():
    """A person edited months to 6 in the review: the computed dates use 6, and a rerun does not undo the edit."""
    res = _monroe()
    decisions = {"Renewal_Option_Length_Mo": {"s": "edited", "v": 6}}
    fields, _ = plan_contract_fields({"Renewal_Option_Length_Mo": 6}, res.terms, res.pricing, decisions=decisions)
    assert fields["End_After_1_Renewal"] == "2027-03-31" and fields["Final_End_Date"] == "2027-09-30"
    kept, notes = review_payload.apply_decisions(fields, decisions)
    assert "Renewal_Option_Length_Mo" not in kept


def test_reviewed_computed_date_is_not_rewritten():
    res = _monroe()
    decisions = {"Final_End_Date": {"s": "edited", "v": "2028-10-31"}}
    fields, _ = plan_contract_fields({}, res.terms, res.pricing, decisions=decisions)
    kept, notes = review_payload.apply_decisions(fields, decisions)
    assert "Final_End_Date" not in kept and any("Final_End_Date in review" in n for n in notes)


def test_review_payload_lists_the_computed_dates():
    res = _monroe()
    fields, _ = plan_contract_fields({}, res.terms, res.pricing)
    derived, _ = derive_term_dates(lambda f: fields.get(f))
    payload = json.loads(review_payload.build(None, res, fields, "Monroe.pdf", derived=derived))
    rows = {r["f"]: r for r in payload["fields"]}
    for k in ("Original_End_Date", "Renewals_Allowed", "End_After_1_Renewal", "Final_End_Date"):
        assert k in rows and rows[k]["w"] is True
    assert rows["Final_End_Date"]["v"] == "2028-09-30" and rows["Final_End_Date"]["src"].startswith("Computed")
    assert rows["Original_End_Date"]["ev"]          # the sentence the date came from is still cited


def test_renewals_left_is_a_running_count():
    """Monroe: paper allows 2, one is already used (CRM has 1 left). A re-ingest must not reset it to 2."""
    res = _monroe()
    contract = {"Renewal_Options_Left": 1, "Original_End_Date": None}
    fields, notes = plan_contract_fields(contract, res.terms, res.pricing)
    assert "Renewal_Options_Left" not in fields and fields["Renewals_Allowed"] == 2
    assert any(n.startswith("Differs, left as is: Renewal_Options_Left is 1") for n in notes)
    # empty on the record -> written as before
    fields, _ = plan_contract_fields({}, res.terms, res.pricing)
    assert fields["Renewal_Options_Left"] == 2


def test_month_end_difference_is_explained_in_the_note():
    """Feb-ending contract: CRM has 28 Feb 2028, the rule gives 29 Feb 2028. Nothing is overwritten; the note says why."""
    contract = {"Final_End_Date": "2028-02-28"}
    from terms_extractor import Finding
    f = lambda k, v: Finding(k, v, "High", "ev", "p.1", "r")
    terms = {"Original_End_Date": f("Original_End_Date", "2027-02-28"), "Renewals_Allowed": f("Renewals_Allowed", 1),
             "Renewal_Option_Length_Mo": f("Renewal_Option_Length_Mo", 12)}
    fields, notes = plan_contract_fields(contract, terms, {})
    assert "Final_End_Date" not in fields and fields["End_After_1_Renewal"] == "2028-02-29"
    assert any("Final_End_Date is 2028-02-28" in n and "2028-02-29" in n and "month-end date kept at month-end" in n for n in notes)

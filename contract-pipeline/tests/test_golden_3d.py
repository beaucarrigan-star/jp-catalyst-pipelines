# Golden tests for Phase 3d (clause finder). Run: python -m pytest tests -q  (from contract-pipeline/)
# Calibration record: tests/calibrate_3d.py prints what surfaces on every packet; the numbers asserted
# here are the calibrated outcome of 28 Sep 2026 (see the Phase 3d design note). Same fixture caveat as
# test_golden_3b_3c: Milwaukee / Hope School / Ramsey are SYNTHETIC until the signed agreements are pulled.
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "con_worker"))
sys.path.insert(0, HERE)

from calibrate_3d import PACKETS  # noqa: E402
from clause_finder import clause_hash, find_clauses, party_roles  # noqa: E402
from packet_extract import extract_packet  # noqa: E402
from clause_finder import plan_clauses  # noqa: E402


def run(name):
    docs, ct, has_acct = PACKETS[name]
    res = extract_packet(docs, ct, contract_has_account=has_acct)
    return find_clauses(res.docs, ct)


def titles(props):
    return [p.title for p in props]


# 1. Allied standard: the golden "Sheboygan excluded from tiered pricing" clause, High, citing Addendum 23
def test_allied_standard_sheboygan_exclusion():
    props, dropped, notes = run("1 Allied standard")
    ex = [p for p in props if "not eligible to be counted toward unit tiered/volume pricing" in p.text]
    assert len(ex) == 1
    assert ex[0].confidence == "High" and ex[0].clause_type == "Special work"
    assert "Addendum 23" in ex[0].source and ex[0].source.endswith("p.2")
    assert len(props) <= 4, titles(props)                # LDS 5% cap and the separate-invoice duty are the only others
    # rate tables, spare rule, term/renewal, signature blocks are all suppressed
    assert not any("$" in p.text and "Per Day" in p.text for p in props)


# 2. Allied Sheboygan: same clause, once (Addendum 22 s.6 and Addendum 23 s.7 collapse), never twice
def test_allied_sheboygan_dedupes_across_addenda():
    props, _, _ = run("2 Allied Sheboygan")
    ex = [p for p in props if "not eligible" in p.text]
    assert len(ex) == 1 and ex[0].confidence == "High"
    assert len(props) <= 4, titles(props)


# 3. Milwaukee (synthetic): itemised monthly invoices with vendor invoices attached; insurance cancellation notice.
#    Term, 60-day termination, payment days, governing law, NTE are 3c fields -> not clauses.
def test_milwaukee_reporting_not_structured_fields():
    props, _, _ = run("3 Milwaukee (synthetic)")
    types = {p.clause_type for p in props}
    assert "Reporting" in types and any("itemized" in p.text for p in props)
    assert all(p.obligor == "us" for p in props)
    assert not any("governed by" in p.text or "sixty (60) days" in p.text for p in props)
    assert len(props) <= 3


# 4. Hope School: everything in the packet is a rate, spare rule, renewal or payment term -> nothing proposed
def test_hope_school_nothing_to_propose():
    props, _, _ = run("4 Hope School (synthetic + real amend)")
    assert props == []


# 5. SCRAM (real master + schedule): the operational duties surface; boilerplate does not; capped for the reviewer
def test_scram_master_surfaces_duties_not_boilerplate():
    props, dropped, notes = run("5 SCRAM (real)")
    text = " || ".join(p.text for p in props)
    for must in ("24/7 monitoring service", "Designate a person", "certificates of insurance",
                 "retain all records", "sufficient number of Trained Employees",
                 "Monitoring Services available for twenty - four (24) hours"):
        assert must in text, must
    for never in ("indemnif", "LIMITATION OF LIABILITY", "arbitration", "Entire Agreement", "force majeure",
                  "Headings", "within ten (10) days of date of invoice"):
        assert never.lower() not in text.lower(), never
    assert 8 <= len(props) <= 15, len(props)
    assert dropped > 50                                  # the long tail is really being dropped
    # section merge: 4.10's three insurance sentences are one row
    assert len([p for p in props if p.clause_type == "Insurance"]) == 1


# 6. Ramsey (synthetic): reporting and staffing obligations, High, citing the page; insurance/termination/NTE are 3c
def test_ramsey_reporting_and_staffing():
    props, _, _ = run("6 Ramsey (synthetic)")
    assert sorted(p.clause_type for p in props) == ["Reporting", "Staffing"]
    assert all(p.confidence == "High" and p.source.endswith("p.2") for p in props)
    rep = next(p for p in props if p.clause_type == "Reporting")
    assert rep.cadence.lower() == "monthly"


# Roles: who is "us" is read off the document, not guessed
def test_party_roles_from_document():
    assert "contractor" in party_roles('between the County of X ("County") and JusticePoint, Inc. ("Contractor").', "Customer")[0]
    ours, theirs = party_roles('Justice Point (hereinafter "Channel Partner") and Allied (hereinafter "Contractor")', "Vendor")
    assert "channel partner" in ours and "contractor" in theirs and "customer" not in ours


# Write-back rules: hash key, rerun no-op, Rejected never re-proposed
def test_plan_clauses_rules():
    props, _, _ = run("6 Ramsey (synthetic)")
    plan, notes = plan_clauses([], props, "C1", "A1")
    assert len(plan["create"]) == 2
    assert all(f["Status"] == "Proposed" and f["Account"] == {"id": "A1"} for f in plan["create"])
    existing = [{"Clause_Hash": f["Clause_Hash"], "Status": "Confirmed"} for f in plan["create"]]
    plan2, _ = plan_clauses(existing, props, "C1", "A1")
    assert plan2["create"] == []                         # rerun is a no-op
    existing[0]["Status"] = "Rejected"
    plan3, notes3 = plan_clauses(existing, props, "C1", "A1")
    assert plan3["create"] == [] and any("previously rejected" in n for n in notes3)
    # the hash is per contract and per normalised text
    assert clause_hash("C1", "a  b") == clause_hash("C1", "A B") != clause_hash("C2", "a b")


# ---------------------------------------------------------------- Phase 3d finish (Opus, 27 Sep 2026)
from clause_finder import _headings, _list_items, derive_due_date  # noqa: E402
from test_golden_3b_3c import doc  # noqa: E402
from packet_extract import PacketDoc  # noqa: E402
from text_norm import normalise  # noqa: E402

# Contract_Clauses fields as returned by getFields on 27 Sep 2026 (CustomModule16)
LIVE_CLAUSE_FIELDS = {"Name", "Clause_Type", "Title", "Clause_Text", "Source_Ref", "Cadence", "Due_Date", "Status",
                      "Confidence", "Clause_Hash", "Contract", "Account"}
LIVE_CLAUSE_TYPES = {"Special work", "Reporting", "Staffing", "Training", "Install", "Service level", "Insurance",
                     "Data & privacy", "Termination", "Other"}


def test_plan_fields_match_live_module():
    for name in PACKETS:
        props, _, _ = run(name)
        plan, _ = plan_clauses([], props, "C1", "A1")
        for f in plan["create"]:
            assert set(f) <= LIVE_CLAUSE_FIELDS, set(f) - LIVE_CLAUSE_FIELDS
            assert f["Name"] and len(f["Name"]) <= 120 and len(f["Clause_Hash"]) <= 64
            assert f["Clause_Type"] in LIVE_CLAUSE_TYPES and f["Confidence"] in ("High", "Medium")


# Hand-off 3: list items under a lead-in are scored one by one, each with the lead's subject
def test_list_items_split_and_carry_subject():
    s = ("Partner shall also: 4.5.1 Designate a person to handle Customer satisfaction matters with AMS; "
         "4.5.2 Provide Customers with all informational materials; and 4.5.3 Investigate all complaints received")
    items = _list_items(s)
    assert len(items) == 3
    assert items[0][0].startswith("Partner shall designate a person")
    assert items[0][1].startswith("Partner shall also: 4.5.1 Designate")
    assert items[2][1].endswith("4.5.3 Investigate all complaints received")
    props, _, _ = run("5 SCRAM (real)")
    des = [p for p in props if "4.5.1 Designate a person" in p.text]
    assert des and "4.5.2" not in des[0].text          # the item no longer drags its siblings along


# Hand-off 2: unnumbered headings are detected, conservatively
def test_unnumbered_headings():
    t = normalise(open(os.path.join(HERE, "fixtures", "allied_add22.txt"), encoding="utf-8").read())
    found = [h[2] for h in _headings(t)]
    for h in ("Scope & Overview", "Term", "Definition", "General Information"):
        assert h in found, found
    # sentence-leading party names / ordinary words are not headings
    t2 = normalise("Signed below. Contractor and Channel Partner agree as follows. Provided Partner pays the fees.")
    assert _headings(t2) == []
    props, _, _ = run("1 Allied standard")
    inv = next(p for p in props if "separate monthly invoice" in p.text)
    assert inv.section == "General Information"


# Hand-off 4: Due_Date only where the wording pins a date down
def test_due_date_rules():
    rep = "Contractor shall provide a monthly utilization report by the tenth business day of each month"
    assert derive_due_date(rep, "2025-01-01", None, "2026-09-27") == "2026-10-14"
    assert derive_due_date(rep, "2027-03-01", None, "2026-09-27") == "2027-03-12"   # future start wins
    assert derive_due_date("within thirty (30) days after the effective date of termination or expiration, Partner shall",
                           "2024-01-10", "2027-01-09") == "2027-02-08"
    assert derive_due_date("deliver within fifteen (15) business days of the Effective Date", "2026-09-25") == "2026-10-16"
    assert derive_due_date("within ten (10) days if any authorised personnel leave", "2024-01-10") == ""
    assert derive_due_date(rep, None, None, None) != ""          # recurring: today is enough
    assert derive_due_date("within 30 days of the Effective Date", None) == ""     # no anchor, no guess
    props, _, _ = find_clauses(extract_packet(*PACKETS["6 Ramsey (synthetic)"][:1], "Customer",
                                              contract_has_account=True).docs,
                               "Customer", start_date="2025-01-01", today="2026-09-27")
    assert next(p for p in props if p.clause_type == "Reporting").due_date == "2026-10-14"


# Hand-off 5: holdout addenda (never used for calibration) yield 0-1 clauses each
def test_holdout_addenda_9_and_19():
    for fx, name in (("allied_add09", "Addendum 9.pdf"), ("allied_add19", "Addendum 19.pdf")):
        res = extract_packet([doc(fx, name)], "Vendor", contract_has_account=False)
        props, _, _ = find_clauses(res.docs, "Vendor")
        assert len(props) <= 1, (fx, titles(props))


# Hand-off 6: other vendors' defined terms are read off the document
def test_vendor_roles_generalise():
    ours, theirs = party_roles('This Agreement between Buddi US LLC ("Supplier") and JusticePoint, Inc. ("Buyer").', "Vendor")
    assert "buyer" in ours and "supplier" in theirs
    ours, theirs = party_roles('SuperCom Inc. ("Licensor") and Justice Point ("Licensee")', "Vendor")
    assert "licensee" in ours and "licensor" in theirs
    text = ('Dealer Agreement between Track Group, Inc. ("Company") and JusticePoint, Inc. ("Dealer").\f'
            '5. Dealer Obligations. Dealer shall provide a monthly activation report to Company by the fifth business '
            'day of each month. Company shall indemnify Dealer against all claims.')
    props, _, _ = find_clauses([PacketDoc(name="Track Group Dealer Agreement.pdf", text=text, doc_type="Base agreement")],
                               "Vendor")
    assert [p.obligor for p in props] == ["us"] and props[0].clause_type == "Reporting"

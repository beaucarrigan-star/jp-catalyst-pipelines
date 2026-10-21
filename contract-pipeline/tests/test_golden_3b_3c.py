# Golden tests for Phase 3b (rates) and 3c (terms). Run: python -m pytest tests -q  (from contract-pipeline/)
#
# Fixtures:
#   * allied_add13/22/23/24, ams_master, ams_schedule, hope_school_amend -- REAL text pulled from the
#     TDPContracts SharePoint on 28 Sep 2026 (Graph PDF conversion: one line per page). The worker will
#     see pdfplumber text instead; the extractors never rely on line breaks, so both work.
#   * SYNTHETIC_milwaukee / SYNTHETIC_hope_school / SYNTHETIC_ramsey -- WRITTEN BY CLAUDE from the golden
#     table's expected values because the signed customer agreements were not on SharePoint or attached
#     in CRM. They prove the rules, not the real documents: replace each with the real packet text when
#     it is pulled and keep the same assertions.
#   * `EXISTING_*` rows are the live Rate_Lines on the golden Contracts as read from Zoho on 28 Sep 2026.

import os
import sys


HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "con_worker"))

from packet_extract import PacketDoc, extract_packet  # noqa: E402
from writeback import plan_contract_fields, plan_rate_lines  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def doc(fixture, name, doc_type=""):
    with open(os.path.join(FIX, fixture + ".txt"), encoding="utf-8") as f:
        text = f.read().replace("\n", "\f")  # fixture: one line per page -> packet_reader's \f joins
    return PacketDoc(name=name, text=text, doc_type=doc_type)


def lines(res):
    return sorted((r.device, r.tier_min, r.tier_max, r.amount, r.effective_date) for r in res.rate_rows)


def row(id_, device, amount, tmin=None, tmax=None, terms="", source=None, eff=None, verified=False):
    return {"id": id_, "Rate_Device": device, "Rate_Daily_Amount": amount, "Tier_Min_Units": tmin,
            "Tier_Max_Units": tmax, "Rate_Terms": terms, "Source_Ref": source, "Effective_Date": eff,
            "Line_Verified": verified, "Superseded_By": None}


EXISTING_ALLIED_STD = [
    row("257", "GPS", 2.82, 401, None, "1-Piece GPS, 401+ units/day (Addendum 24, eff. Feb 1 2026)"),
    row("258", "Remote Breath", 3.50, terms="Allied RB"),
    row("259", "CAM", 8.53, terms="Allied CAM wireless"),
    row("260", "CAM Landline", 7.60, terms="Allied CAM landline"),
    row("092", "GPS", 3.05, 301, 400), row("093", "GPS", 3.10, 51, 300), row("094", "GPS", 3.15, 1, 50),
]
EXISTING_SHEBOYGAN = [
    row("262", "GPS", 2.55, terms="Allied GPS, Sheboygan pricing"), row("263", "Remote Breath", 3.50),
    row("264", "CAM", 8.53), row("265", "CAM Landline", 7.60),
]
EXISTING_SCRAM = [row("253", "CAM Landline", 6.27), row("254", "CAM", 6.94), row("255", "Remote Breath", 3.82)]

ALLIED_STD_PACKET = [
    doc("allied_add13", "Addendum 13 - Tiered Pricing and Spare Fee 2022.pdf"),
    doc("allied_add23", "Addendum 23 w Exhibit 2-Justice Point for Sheboygan County Sheriff.pdf"),
    doc("allied_add24", "Addendum 24 - New Rate 401 plus and Upper Spares.pdf"),
]


# ------------------------------------------------------------------ 1. Allied standard
def test_allied_standard_rates_and_supersession():
    res = extract_packet(ALLIED_STD_PACKET, "Vendor", contract_has_account=False)
    assert lines(res) == [
        ("GPS", 1, 50, 3.15, "2022-06-01"), ("GPS", 51, 300, 3.10, "2022-06-01"),
        ("GPS", 301, 400, 3.05, "2022-06-01"), ("GPS", 401, None, 2.82, "2026-02-01"),
        ("GPS", 401, None, 2.92, "2022-06-01"),
    ]
    assert [(o.amount, n.amount) for o, n in res.superseded] == [(2.92, 2.82)]
    # Addendum 23 is Sheboygan-program pricing: not applied to the standard record
    assert any("specific to the program" in n for n in res.notes)
    assert res.pricing["spare_pct"] == 35 and res.pricing["spare_fee"] == 1.25

    plan, notes = plan_rate_lines(EXISTING_ALLIED_STD, res.rate_rows, res.superseded, "Vendor")
    # the four current tier rows are adopted (citation filled, amount untouched); only 2.92 is inserted
    assert set(plan["update"]) == {"094", "093", "092", "257"}
    assert all("Rate_Daily_Amount" not in f for f in plan["update"].values())
    assert plan["update"]["257"]["Effective_Date"] == "2026-02-01"
    assert "Addendum 24" in plan["update"]["257"]["Source_Ref"]
    assert len(plan["insert"]) == 1 and plan["insert"][0][1]["Rate_Daily_Amount"] == 2.92
    assert plan["supersede"] == [(("new", 0), ("id", "257"))]
    # RB / CAM rows (hand-entered, no Source_Ref) are left alone
    assert not {"258", "259", "260"} & set(plan["update"])

    fields, fnotes = plan_contract_fields({"Ingest_Status": None}, res.terms, res.pricing,
                                          [(d.doc_type, d.effective_date) for d in res.docs])
    assert "Spare_Device_Clause" not in fields  # 35% is not a picklist value
    assert fields["Spare_Clause_Detail"].startswith("Spares above 35% billed $1.25/day/unit")
    assert "Addendum 24" in fields["Spare_Clause_Detail"]


def test_allied_add24_applies_to_contracts():
    from doc_classifier import extract_applies_to_contracts
    d = ALLIED_STD_PACKET[2]
    assert extract_applies_to_contracts(d.text) == "592,835,918,943,966,993,1023"


# ------------------------------------------------------------------ 2. Allied Sheboygan
def test_allied_sheboygan():
    packet = [doc("allied_add22", "Addendum 22 - Justice Point- Sheboygan County Sheriff.pdf"),
              doc("allied_add23", "Addendum 23 w Exhibit 2-Justice Point for Sheboygan County Sheriff.pdf")]
    res = extract_packet(packet, "Vendor", contract_has_account=True)
    assert lines(res) == [
        ("CAM", None, None, 8.53, "2025-12-11"), ("CAM Landline", None, None, 7.60, "2025-12-11"),
        ("GPS", None, None, 2.55, "2025-07-25"), ("Remote Breath", None, None, 3.50, "2025-07-25"),
    ]
    assert not any(r.tier_min for r in res.rate_rows)  # no tier lines on this record
    plan, notes = plan_rate_lines(EXISTING_SHEBOYGAN, res.rate_rows, res.superseded, "Vendor")
    assert plan["insert"] == [] and plan["supersede"] == []
    assert set(plan["update"]) == {"262", "263", "264", "265"}
    assert "Addendum 22" in plan["update"]["263"]["Source_Ref"]   # RB sourced from 22, not the standard packet


# ------------------------------------------------------------------ 3. Milwaukee (synthetic text)
def test_milwaukee_cost_plus():
    res = extract_packet([doc("SYNTHETIC_milwaukee", "Milwaukee County Pretrial EM Agreement 2024.pdf", "Base agreement")],
                         "Customer", contract_has_account=True)
    assert res.rate_rows == []  # no fixed GPS rate invented
    fields, _ = plan_contract_fields({}, res.terms, res.pricing)
    assert fields["Rate_Basis"] == "Cost-plus" and fields["Admin_Rate_Pct"] == 12.0
    assert fields["Contract_Start_Date"] == "2024-01-01" and fields["Contract_End_Date"] == "2026-12-31"
    assert fields["Auto_Renews"] == "No" and fields["Governing_State"] == "WI"
    assert fields["Termination_Convenience_Days"] == 60 and fields["Payment_Terms_Days"] == 30
    assert "Notice_Period_Days" not in fields  # the 30-day insurance notice must not leak in


# ------------------------------------------------------------------ 4. Hope School (synthetic base + real amendment)
def test_hope_school():
    packet = [doc("SYNTHETIC_hope_school", "Hope School EM Contract.pdf", "Base agreement"),
              doc("hope_school_amend", "AMENDMENT No. 1 to Electronic Monitoring Contract _ Hope Learning Academy.pdf")]
    res = extract_packet(packet, "Customer", contract_has_account=True)
    got = sorted((r.device, r.amount, r.ownership) for r in res.rate_rows)
    assert got == [("GPS", 2.50, "Purchased"), ("GPS", 4.95, "")]  # $55 Freedom Charger is one-time: no line
    purchased = next(r for r in res.rate_rows if r.ownership)
    assert purchased.terms.startswith("Purchased units:")
    fields, notes = plan_contract_fields({}, res.terms, res.pricing,
                                         [(d.doc_type, d.effective_date) for d in res.docs])
    assert (fields["Contract_Start_Date"], fields["Contract_End_Date"]) == ("2025-07-01", "2027-06-30")
    assert fields["Auto_Renews"] == "Yes" and fields["Notice_Period_Days"] == 90
    assert fields["Renewal_Options_Left"] == 2 and fields["Renewal_Option_Length_Mo"] == 12
    assert fields["Spare_Device_Clause"] == "20% threshold"
    assert fields["Amendment_Count"] == 1
    # 'Purchased units' is not in the Rate_Basis picklist yet -> Note, not a rejected write
    assert "Rate_Basis" not in fields and any("Purchased units" in n for n in notes)
    # IL is not in the Governing_State picklist yet
    assert "Governing_State" not in fields and any("'IL'" in n for n in notes)


# ------------------------------------------------------------------ 5. SCRAM (real agreement + schedule)
SCRAM_PACKET = [doc("ams_master", "Justice Point_Master SP Agreement_Expires 20270109.pdf", "Base agreement"),
                doc("ams_schedule", "Justice Point_Product and Service Schedule_Expires 20270109.pdf", "Rate sheet")]


def test_scram_rates():
    res = extract_packet(SCRAM_PACKET, "Vendor", contract_has_account=False)
    got = {r.device: r.amount for r in res.rate_rows}
    assert got["CAM Landline"] == 6.27 and got["Remote Breath"] == 3.82  # matches CRM to the cent
    assert got["CAM"] == 8.62  # CRM holds 6.94 -> conflict, see below
    plan, notes = plan_rate_lines(EXISTING_SCRAM, res.rate_rows, res.superseded, "Vendor")
    assert set(plan["update"]) == {"253", "255"} and plan["insert"] == []
    assert plan["update"]["253"]["Effective_Date"] == "2024-01-10"
    assert any(n.startswith("CONFLICT") and "$6.94" in n and "$8.62" in n for n in notes)


def test_scram_terms():
    res = extract_packet(SCRAM_PACKET, "Vendor", contract_has_account=False)
    fields, notes = plan_contract_fields({}, res.terms, res.pricing)
    assert fields["Contract_Start_Date"] == "2024-01-10"
    assert fields["Contract_End_Date"] == "2027-01-09"          # from the file name (Medium)
    assert fields["Auto_Renews"] == "Yes" and fields["Renewal_Option_Length_Mo"] == 12
    assert fields["Notice_Period_Days"] == 30 and fields["Payment_Terms_Days"] == 30
    assert fields["Invoice_Cadence"] == "Monthly" and fields["Insurance_Limit"] == 1000000.0
    assert "Escalator_Pct" not in fields and any("CPI" in n for n in notes)
    assert any("'CO'" in n for n in notes)                       # Colorado not in picklist


# ------------------------------------------------------------------ 6. Ramsey (synthetic text)
def test_ramsey():
    res = extract_packet([doc("SYNTHETIC_ramsey", "Ramsey County EM Agreement.pdf", "Base agreement")],
                         "Customer", contract_has_account=True)
    assert [(r.device, r.amount) for r in res.rate_rows] == [("Remote Breath", 3.92)]
    fields, _ = plan_contract_fields({}, res.terms, res.pricing)
    assert (fields["Contract_Start_Date"], fields["Contract_End_Date"]) == ("2025-12-03", "2027-12-31")
    assert fields["Governing_State"] == "MN" and fields["Auto_Renews"] == "No"
    assert fields["Payment_Terms_Days"] == 35 and fields["Termination_Convenience_Days"] == 30
    assert fields["Not_To_Exceed"] == 250000.0


# ------------------------------------------------------------------ cross-set rules
def test_rerun_is_noop():
    """Second run: rows now carry Source_Ref + Effective_Date exactly as the first run wrote them."""
    res = extract_packet(ALLIED_STD_PACKET, "Vendor", contract_has_account=False)
    after = []
    for r in res.rate_rows:
        after.append(row("x{}".format(len(after)), r.device, r.amount, r.tier_min, r.tier_max, r.terms, r.source, r.effective_date))
    plan, notes = plan_rate_lines(after, res.rate_rows, res.superseded, "Vendor")
    assert plan["update"] == {} and plan["insert"] == []


def test_verified_row_survives_changed_value():
    res = extract_packet(ALLIED_STD_PACKET, "Vendor", contract_has_account=False)
    cur = next(r for r in res.rate_rows if r.tier_min == 401 and r.amount == 2.82)
    existing = [row("v1", "GPS", 2.75, 401, None, "", cur.source, cur.effective_date, verified=True)]
    plan, notes = plan_rate_lines(existing, [cur], [], "Vendor")
    assert "v1" not in plan["update"] and any(n.startswith("Verified line kept") for n in notes)


def test_verified_contract_fields_not_overwritten():
    res = extract_packet([doc("SYNTHETIC_ramsey", "Ramsey.pdf", "Base agreement")], "Customer", True)
    fields, notes = plan_contract_fields({"Ingest_Status": "Ingested", "Contract_End_Date": "2027-06-30"},
                                         res.terms, res.pricing)
    assert fields == {} and any("Differs, left as is: Contract_End_Date" in n for n in notes)


def test_high_confidence_overwrites_unverified_but_medium_does_not():
    res = extract_packet(SCRAM_PACKET, "Vendor", False)
    fields, notes = plan_contract_fields({"Contract_End_Date": "2026-12-31", "Notice_Period_Days": 60},
                                         res.terms, res.pricing)
    assert "Contract_End_Date" not in fields            # Medium (file name) never overwrites
    assert fields["Notice_Period_Days"] == 30            # High overwrites an unverified value, noted
    assert any(n.startswith("Changed: Notice_Period_Days 60 -> 30") for n in notes)


# ------------------------------------------------------------------ holdout: real addenda NOT used to write the rules
def test_holdout_add09_cam_prices():
    res = extract_packet([doc("allied_add09", "Addendum 9 2021 - AMS CAM - FULLY EXECUTED.pdf")], "Vendor", False)
    assert lines(res) == [("CAM", None, None, 7.75, "2021-02-24"), ("CAM Landline", None, None, 6.75, "2021-02-24")]


def test_holdout_spare_chain_add13_add19_add24():
    packet = ALLIED_STD_PACKET + [doc("allied_add19", "Addendum 19_Increase Spare Level 2024 (002).pdf")]
    res = extract_packet(packet, "Vendor", False)
    assert res.pricing["spare_pct"] == 35  # 20% (Add 13, 2022) -> 25% (Add 19, Jun 2024) -> 35% (Add 24, 2026)
    only19 = extract_packet([doc("allied_add19", "Addendum 19_Increase Spare Level 2024 (002).pdf")], "Vendor", False)
    assert only19.pricing["spare_pct"] == 25 and only19.pricing["spare_eff"] == "2024-06-24"

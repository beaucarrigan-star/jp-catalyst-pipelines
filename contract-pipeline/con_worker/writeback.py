# writeback.py -- turns Phase 3b/3c proposals into Zoho changes under the Phase 0 rules.
# Pure functions: they take what Zoho currently holds and return a plan + Note lines. main.py applies
# the plan. Keeping the rules here (not in main.py) is what makes the golden tests runnable offline.
#
# Rate_Lines rules (Phase 0 spec, "Upsert keys and supersession"):
#   key = Rate_Device + Tier_Min_Units (null = 0) + Effective_Date + parent Contract
#         (+ ownership: a "Purchased units:" line and a leased line for the same device are different
#          keys -- Hope School has both; see build note)
#   * same key, same amount            -> no-op
#   * same key, new amount             -> update, unless Line_Verified (then Note only)
#   * hand-entered rows (no Source_Ref) are never changed ... with one exception that the golden set
#     requires: an unverified hand row whose device+tier+amount equal the packet's CURRENT line is
#     ADOPTED -- only its empty Source_Ref / Effective_Date / Confidence are filled; amount and
#     Rate_Terms are untouched. That is how "CAM $6.27 ... now with Source_Ref and effective date" and
#     the four Allied tier rows get their citations without duplicate rows.
#   * a hand row with the same device+tier but a DIFFERENT amount from the packet's current line is a
#     conflict: nothing is written for that device+tier, the Note lists both values for a person.
#   * new effective date for a device+tier already present -> insert the new row and set Superseded_By
#     on the older row to the new row's id. Nothing is ever deleted.
#
# Contract field rules ("Contract term fields"):
#   * Ingest_Status = "Ingested" is the verified state (the person flips Needs Review -> Ingested);
#     a verified contract's fields are never overwritten -- differences go to the Note.
#   * otherwise: empty field -> write (High or Medium); non-empty and different -> overwrite only on
#     High confidence, and the Note records old -> new; Low findings are Note-only.
#   * picklist values not present in the live picklist are Note-only (Zoho would reject them).

from datetime import date as _date

from rate_extractor import is_current
from text_norm import add_term_months

VERIFIED_STATUSES = {"Ingested", "Verified"}

PICKLISTS = {
    "Rate_Basis": {"Fixed", "Cost-plus", "Tiered", "Per-unit", "Other"},
    "Spare_Device_Clause": {"20% threshold", "30% threshold", "Not applicable", "No spare allowance", "Unknown"},
    "Auto_Renews": {"Yes", "No", "Not stated"},
    "Invoice_Cadence": {"Monthly", "Quarterly", "Annual", "Other"},
    "Governing_State": {"WI", "IA", "ID", "MI", "MN", "MO", "OH", "VA", "WV", "WY", "N/A"},
}


def _ownership_of_row(row):
    return "Purchased" if "purchased" in (row.get("Rate_Terms") or "").lower() else ""


def _row_key(row, with_date=True):
    k = (row.get("Rate_Device"), row.get("Tier_Min_Units") or 0, _ownership_of_row(row))
    return k + ((row.get("Effective_Date"),) if with_date else ())


def _prop_key(p, with_date=True):
    k = (p.device, p.tier_min or 0, p.ownership)
    return k + ((p.effective_date,) if with_date else ())


def _amount_eq(a, b):
    return a is not None and b is not None and abs(float(a) - float(b)) < 0.005


def _new_row_fields(p, rate_type):
    return {
        "Rate_Device": p.device,
        "Rate_Daily_Amount": p.amount,
        "Tier_Min_Units": p.tier_min,
        "Tier_Max_Units": p.tier_max,
        "Effective_Date": p.effective_date,
        "Source_Ref": p.source[:255],
        "Confidence": p.confidence,
        "Rate_Terms": p.terms[:255],
        "Rate_Type": rate_type,
        "Line_Verified": False,
    }


def plan_rate_lines(existing_rows, rows, superseded, counterparty_type):
    """existing_rows: Zoho Rate_Lines subform rows (dicts). rows/superseded: from resolve_current().
    Returns (plan, notes). plan = {"update": {row_id: fields}, "insert": [(ref, fields)],
    "supersede": [(old_ref, new_ref)]} where a ref is ("id", zoho_id) or ("new", index into insert)."""
    rate_type = "Vendor cost" if counterparty_type == "Vendor" else "Customer rate"
    plan = {"update": {}, "insert": [], "supersede": []}
    notes = []
    ref_of = {}          # id(proposal) -> ref
    adopted = set()      # existing row ids already claimed in this run

    by_key = {}
    for r in existing_rows:
        if r.get("Source_Ref"):
            by_key.setdefault(_row_key(r), []).append(r)
    hand = [r for r in existing_rows if not r.get("Source_Ref")]

    for p in rows:
        k = _prop_key(p)
        match = next((r for r in by_key.get(k, []) if r["id"] not in adopted), None)
        if match:
            adopted.add(match["id"])
            ref_of[id(p)] = ("id", match["id"])
            if _amount_eq(match.get("Rate_Daily_Amount"), p.amount):
                continue
            if match.get("Line_Verified"):
                notes.append("Verified line kept: {} tier {} eff {} is ${:.2f} in CRM, packet says ${:.2f} ({})".format(
                    p.device, p.tier_min or "-", p.effective_date, float(match["Rate_Daily_Amount"]), p.amount, p.source))
                continue
            plan["update"][match["id"]] = {"Rate_Daily_Amount": p.amount, "Source_Ref": p.source[:255],
                                           "Confidence": p.confidence, "Rate_Terms": p.terms[:255]}
            notes.append("Rate changed: {} tier {} eff {} ${:.2f} -> ${:.2f} ({})".format(
                p.device, p.tier_min or "-", p.effective_date, float(match["Rate_Daily_Amount"]), p.amount, p.source))
            continue

        current = is_current(p, superseded)
        same_group_hand = [r for r in hand if _row_key(r, False) == _prop_key(p, False) and r["id"] not in adopted]
        if current and same_group_hand:
            eq = next((r for r in same_group_hand if _amount_eq(r.get("Rate_Daily_Amount"), p.amount)), None)
            if eq:
                adopted.add(eq["id"])
                ref_of[id(p)] = ("id", eq["id"])
                if eq.get("Line_Verified"):
                    continue
                fill = {}
                if not eq.get("Source_Ref"):
                    fill["Source_Ref"] = p.source[:255]
                if not eq.get("Effective_Date") and p.effective_date:
                    fill["Effective_Date"] = p.effective_date
                if not eq.get("Confidence"):
                    fill["Confidence"] = p.confidence
                if fill:
                    plan["update"][eq["id"]] = fill
                continue
            r0 = same_group_hand[0]
            notes.append("CONFLICT, nothing written: hand-entered {}{} is ${:.2f}, packet's current line is ${:.2f} "
                         "({}; {}). Check which is right and fix the row by hand.".format(
                             p.device, " tier {}".format(p.tier_min) if p.tier_min else "",
                             float(r0.get("Rate_Daily_Amount") or 0), p.amount, p.source, p.terms))
            ref_of[id(p)] = None
            continue

        plan["insert"].append((("new", len(plan["insert"])), _new_row_fields(p, rate_type)))
        ref_of[id(p)] = ("new", len(plan["insert"]) - 1)

    for old, new in superseded:
        o, n = ref_of.get(id(old)), ref_of.get(id(new))
        if not o or not n:
            continue
        if o[0] == "id":
            row = next((r for r in existing_rows if r["id"] == o[1]), {})
            if row.get("Superseded_By"):
                continue
            if row.get("Line_Verified"):
                notes.append("Verified line {} ${:.2f} is superseded by {} (eff {}) -- mark it by hand".format(
                    old.device, old.amount, new.source, new.effective_date))
                continue
        plan["supersede"].append((o, n))
    return plan, notes


DERIVED_SRC = "computed from Original_End_Date, Renewals_Allowed and Renewal_Option_Length_Mo"


def _as_date(v):
    try:
        return _date.fromisoformat(str(v)[:10]) if v not in (None, "", "-None-") else None
    except ValueError:
        return None


def _as_int(v):
    try:
        return int(float(v)) if v not in (None, "", "-None-") else None
    except (TypeError, ValueError):
        return None


def make_eff(contract, updates, decisions):
    """The value each field will have once this run is applied: a person's decision wins, then this run's
    planned write, then what the record holds. A rejected field counts as empty."""
    def eff(fld):
        d = (decisions or {}).get(fld)
        if d:
            return None if d.get("s") == "rejected" else d.get("v")
        return updates[fld] if fld in updates else contract.get(fld)
    return eff


def derive_term_dates(eff):
    """Pure. The two dates that are computed, never read from paper:
        End_After_1_Renewal = Original_End_Date + 1 renewal period
        Final_End_Date      = Original_End_Date + Renewals_Allowed renewal periods
    Returns ({field: (iso_date, rule)}, [notes]). Blank in -> nothing out (no guessing)."""
    orig, allowed, months = _as_date(eff("Original_End_Date")), _as_int(eff("Renewals_Allowed")), _as_int(eff("Renewal_Option_Length_Mo"))
    out, notes = {}, []
    if orig is None:
        if allowed is not None or months is not None:
            notes.append("End dates after renewal not computed: Original_End_Date is empty (needs the base agreement's term end).")
        return out, notes
    if allowed is None:
        notes.append("End dates after renewal not computed: Renewals_Allowed is empty.")
        return out, notes
    if allowed == 0:
        out["Final_End_Date"] = (orig.isoformat(), "no renewals allowed, so the final end is the original end")
        return out, notes
    if not months:
        notes.append("End dates after renewal not computed: Renewal_Option_Length_Mo is empty ({} renewal(s) allowed).".format(allowed))
        return out, notes
    import calendar
    tail = "; month-end date kept at month-end" if orig.day == calendar.monthrange(orig.year, orig.month)[1] else ""
    out["End_After_1_Renewal"] = (add_term_months(orig, months).isoformat(), "Original_End_Date + {} months{}".format(months, tail))
    out["Final_End_Date"] = (add_term_months(orig, months * allowed).isoformat(),
                             "Original_End_Date + {} renewal(s) x {} months{}".format(allowed, months, tail))
    return out, notes


def plan_contract_fields(contract, findings, pricing, documents=None, decisions=None):
    """contract: current Contracts record (dict). findings: {field: terms_extractor.Finding}.
    pricing: rate_extractor.packet_pricing() dict. documents: [(doc_type, effective_date)] for the
    amendment count. Returns (updates, notes)."""
    locked = contract.get("Ingest_Status") in VERIFIED_STATUSES
    updates, notes = {}, []

    def propose(fld, value, conf, source, rule):
        if value is None:
            return
        if conf == "Low":
            notes.append("Low-confidence, not written: {} = {} ({}; {})".format(fld, value, source, rule))
            return
        if fld in PICKLISTS and value not in PICKLISTS[fld]:
            notes.append("Not written: {} = '{}' is not in the picklist -- add it in Setup > Fields, then rerun "
                         "({}; {})".format(fld, value, source, rule))
            return
        cur = contract.get(fld)
        if cur in (None, "", "-None-", []):
            if locked:
                notes.append("Verified contract, not written: {} = {} ({})".format(fld, value, source))
                return
            updates[fld] = value
            return
        if _same(cur, value):
            return
        if locked or conf != "High" or fld == "Renewal_Options_Left":
            # Renewal_Options_Left is a running count (the paper only gives the total, kept in Renewals_Allowed),
            # so a value already on the record is never replaced by the packet's total.
            notes.append("Differs, left as is: {} is {} in CRM, packet says {} ({}, {}; {})".format(
                fld, cur, value, conf, source, rule))
            return
        updates[fld] = value
        notes.append("Changed: {} {} -> {} ({}; {})".format(fld, cur, value, source, rule))

    for fld, f in findings.items():
        if fld == "Escalator_Note":
            notes.append("Escalator: CPI-based annual adjustment, no fixed % ({}) -- Escalator_Pct left empty".format(f.source))
            continue
        propose(fld, f.value, f.confidence, f.source, f.rule)

    if pricing.get("rate_basis"):
        propose("Rate_Basis", pricing["rate_basis"], "High", pricing.get("rate_basis_file") or "", "rate basis")
        if pricing.get("admin_rate_pct") is not None:
            propose("Admin_Rate_Pct", pricing["admin_rate_pct"], "High", pricing.get("rate_basis_file") or "", "cost-plus %")
    if pricing.get("spare_pct") is not None:
        pct, fee = pricing["spare_pct"], pricing.get("spare_fee")
        pick = "{}% threshold".format(pct)
        detail = "Spares above {}%{} ({}, eff. {})".format(
            pct, " billed ${:.2f}/day/unit".format(fee) if fee else "", pricing.get("spare_file"), pricing.get("spare_eff"))
        if pick in PICKLISTS["Spare_Device_Clause"]:
            propose("Spare_Device_Clause", pick, "High", pricing.get("spare_file") or "", "spare threshold")
        else:
            notes.append("Spare threshold {}% has no picklist value -- written to Spare_Clause_Detail".format(pct))
        propose("Spare_Clause_Detail", detail, "High", pricing.get("spare_file") or "", "spare rule")

    if documents:
        amends = sorted(d for t, d in documents if t in ("Amendment", "Addendum") and d)
        n = len([1 for t, _ in documents if t in ("Amendment", "Addendum")])
        if n:
            propose("Amendment_Count", n, "Medium", "Contract_Documents", "count of amendment/addendum files")
        if amends:
            propose("Last_Amendment_Date", amends[-1], "Medium", "Contract_Documents", "latest amendment date")
    # Computed end dates. Inputs are the values the record will hold after this run (see make_eff), so a
    # person's reviewed Original_End_Date / Renewals_Allowed / months are what the dates are built from.
    eff = make_eff(contract, updates, decisions)
    derived, dnotes = derive_term_dates(eff)
    notes.extend(dnotes)
    for fld, (val, rule) in derived.items():
        propose(fld, val, "Medium", DERIVED_SRC, rule)
    orig, cur_end = _as_date(eff("Original_End_Date")), _as_date(eff("Contract_End_Date"))
    if orig and cur_end and cur_end > orig:
        notes.append("Current term end {} is after the original end {}: an amendment extended the term or a renewal "
                     "was used. Check Renewal_Options_Left.".format(cur_end.isoformat(), orig.isoformat()))
    return updates, notes


def _same(a, b):
    try:
        return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()

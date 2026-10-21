# review_payload.py -- Phase 4 (27 Sep 2026). The JSON the Contract Review widget reads.
#
# Stored on Contracts.Ingest_Review (Multi Line, large, 32000 chars). The worker writes the "fields"
# and "rates" blocks on every ingest; the widget writes the "decisions" block when a person
# Confirms / Edits / Rejects a contract field. The worker keeps "decisions" across reruns and obeys
# them: a field a person has decided is never rewritten by a later ingest (rejected stays empty,
# confirmed/edited keeps the person's value) -- the would-be change goes to the Note instead.
#
# Rate_Lines and Contract_Clauses carry their own review state (Line_Verified / Superseded_By,
# Status), so "rates" here is only the source snippet for each extracted line, keyed the same way
# writeback.py keys Rate_Lines rows (device | tier min | ownership | effective date).
# Pure; no Zoho.

import datetime as dt
import json

MAX_LEN = 32000
VERSION = 1


def parse(raw):
    """Ingest_Review text -> dict (empty dict on blank / bad JSON)."""
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except (TypeError, ValueError):
        return {}


def decisions_of(raw):
    return (parse(raw).get("decisions") or {}).get("fields") or {}


def apply_decisions(field_updates, decisions):
    """Drop every planned field write a person has already decided. Returns (updates, notes)."""
    kept, notes = {}, []
    for fld, val in field_updates.items():
        d = decisions.get(fld)
        if not d:
            kept[fld] = val
            continue
        state = d.get("s")
        if state == "rejected":
            notes.append("Rejected in review, not re-written: {} = {}".format(fld, val))
        elif str(d.get("v")) != str(val):
            notes.append("{} in review ({}), left as is: packet now says {}".format(
                fld, "confirmed" if state == "confirmed" else "edited", val))
        # same value as the person's decision: nothing to do, nothing to say
    return kept, notes


def rate_key(device, tier_min, ownership, effective_date):
    return "{}|{}|{}|{}".format(device or "", int(tier_min or 0), "Purchased" if ownership else "", effective_date or "")


def _clip(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def build(prior_raw, result, field_updates, packet_name, is_current=None, now=None, derived=None):
    """result: packet_extract.PacketResult. field_updates: what this run writes to the Contract.
    is_current: rate_extractor.is_current(p, superseded) or None. Returns the JSON string."""
    prior = parse(prior_raw)
    now = now or dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    def field_row(fld, value, conf, source, evidence, rule):
        return {"f": fld, "v": value, "c": conf, "src": source or "", "ev": evidence or "", "rule": rule or "",
                "w": fld in field_updates}

    fields = []
    for fld, f in (result.terms or {}).items():
        if fld == "Escalator_Note":
            fields.append(field_row("Escalator_Pct", None, f.confidence, f.source, f.evidence,
                                    "CPI-based annual adjustment, no fixed %"))
            continue
        fields.append(field_row(fld, f.value, f.confidence, f.source, f.evidence, f.rule))

    pr = result.pricing or {}
    if pr.get("rate_basis"):
        src = pr.get("rate_basis_file") or ""
        fields.append(field_row("Rate_Basis", pr["rate_basis"], "High", src, pr.get("rate_basis_evidence"), "rate basis"))
        if pr.get("admin_rate_pct") is not None:
            fields.append(field_row("Admin_Rate_Pct", pr["admin_rate_pct"], "High", src,
                                    pr.get("rate_basis_evidence"), "cost-plus %"))
    if pr.get("spare_pct") is not None:
        src = pr.get("spare_file") or ""
        pick = "{}% threshold".format(pr["spare_pct"])
        fields.append(field_row("Spare_Device_Clause", pick, "High", src, pr.get("spare_evidence"), "spare threshold"))
        if "Spare_Clause_Detail" in field_updates:
            fields.append(field_row("Spare_Clause_Detail", field_updates["Spare_Clause_Detail"], "High", src,
                                    pr.get("spare_evidence"), "spare rule"))
    for fld, (val, rule) in (derived or {}).items():
        fields.append(field_row(fld, val, "Medium", "Computed from Original_End_Date, Renewals_Allowed and "
                                "Renewal_Option_Length_Mo", "", rule))
    for fld in ("Amendment_Count", "Last_Amendment_Date"):
        if fld in field_updates:
            fields.append(field_row(fld, field_updates[fld], "Medium", "Contract_Documents", "",
                                    "from the packet's document index"))

    rates = []
    for p in result.rate_rows or []:
        rates.append({
            "k": rate_key(p.device, p.tier_min, p.ownership, p.effective_date),
            "src": p.source or "", "ev": p.label or "", "c": p.confidence,
            "cur": bool(is_current(p, result.superseded)) if is_current else True,
        })

    out = {"v": VERSION, "at": now, "packet": packet_name, "fields": fields, "rates": rates,
           "decisions": prior.get("decisions") or {"fields": {}}}
    return _fit(out)


def _fit(out):
    """Keep the JSON under the field cap: shorten snippets first, then drop them."""
    for n in (400, 240, 120, 0):
        for row in out["fields"] + out["rates"]:
            row["ev"] = _clip(row["ev"], n) if n else ""
        s = json.dumps(out, separators=(",", ":"), default=str)
        if len(s) <= MAX_LEN:
            return s
    out["rates"] = []
    return json.dumps(out, separators=(",", ":"), default=str)[:MAX_LEN]

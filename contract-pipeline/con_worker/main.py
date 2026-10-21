# con_worker -- job function, contract-pipeline Catalyst project.
#
# Phase 2 scaffold + Phase 3b (rates) + Phase 3c (terms) + Phase 3d (clauses), 27-28 Sep 2026.
# Phase 4 (27 Sep 2026): Ingest_Review JSON for the Contract Review widget (review_payload.py).
# Reads the packet, classifies and dates each file, indexes it on Contract_Documents, extracts rate
# lines and term fields, writes them under the Phase 0 rules (writeback.py), and posts the summary Note.
# Clauses (Phase 3d, clause_finder.py) are proposed as Contract_Clauses rows, Status = Proposed.
#
# Idempotent: Packet_Hash (sha256 of the zip) short-circuits an unchanged packet ("already read
# earlier"). Contract_Documents key = parent + File_Name + sha1(bytes). Rate_Lines and Contract fields
# follow writeback.py's rules, so a rerun of a changed packet only moves what changed and never touches
# Verified rows or a Verified (Ingest_Status = Ingested) contract.
# No AI tokens at runtime; no stub Solicitations (Phase 0 decisions).
#
# 28 Sep 2026 fix: _upsert_contract_document no longer puts File_Name in the COQL (apostrophes in file
# names caused "400 SYNTAX_ERROR near Contract, column 43").
#
# 9 Oct 2026: vendor vs customer separation. Counterparty_Type picks the extraction profile; a blank
# value now stops the ingest with a Note instead of silently reading the packet as Customer. New action
# new_vendor_contract_from_packet (button on Vendors) creates a Counterparty_Type = Vendor contract;
# new_contract_from_packet (Accounts) stays Customer-only.

import datetime as dt
import io
import logging
import zipfile

import zoho_client as zc
from doc_classifier import classify, extract_applies_to_contracts
from packet_extract import PacketDoc, extract_packet
from packet_reader import READABLE_EXT, read_packet
from writeback import derive_term_dates, make_eff, plan_contract_fields, plan_rate_lines
from clause_finder import find_clauses, plan_clauses
from rate_extractor import is_current
import review_payload

# Phase 3d (clauses): designed and calibrated on the golden set 28 Sep 2026 (Fable), finished 27 Sep 2026
# (Opus): list items, unnumbered headings, Due_Date, holdout + vendor-role tests, deploy gate. On = each
# proposal is written as a Contract_Clauses row with Status = Proposed for a person to Confirm/Reject;
# a rerun only adds new hashes and never re-proposes a Rejected one. Set False to go back to Note-only.
WRITE_CLAUSES = True

log = logging.getLogger()
# 20 Oct 2026: Catalyst runs the root logger at DEBUG, and pdfminer (under pdfplumber) logs every token it
# parses -- millions of lines on a vector-heavy PDF, which made Sawyer Co HHS run for 10+ minutes.
for _noisy in ("pdfminer", "pdfplumber", "PIL", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

NO_ATTACHMENTS_MSG = ("No readable attachment on this record -- nothing to ingest. Attach the packet as a "
                      ".zip, or the signed agreement as a PDF / Word / Excel file, and press the button again.")

CONTRACT_FIELDS = [
    "Ingest_Status", "Packet_Hash", "Account", "Counterparty_Type", "Rate_Lines",
    "Contract_Start_Date", "Original_End_Date", "Contract_End_Date", "Auto_Renews", "Renewals_Allowed",
    "Renewal_Options_Left", "Renewal_Option_Length_Mo", "End_After_1_Renewal", "Final_End_Date", "Notice_Period_Days", "Termination_Convenience_Days", "Payment_Terms_Days",
    "Invoice_Cadence", "Not_To_Exceed", "Escalator_Pct", "Governing_State", "Insurance_Limit",
    "Rate_Basis", "Admin_Rate_Pct", "Spare_Device_Clause", "Spare_Clause_Detail", "Amendment_Count",
    "Last_Amendment_Date", "Ingest_Review",
]
NOTE_LIMIT = 30000  # Zoho Note_Content practical cap; the Ingest_Summary field (2000) gets the head


def handler(job_request, context):
    """Catalyst python_3_9 Job function entry point (same signature as the live sol_worker): params come
    from job_request.get_job_param, and the job must be closed explicitly or it runs to the timeout."""
    record_id = str(job_request.get_job_param("recordId"))
    action = job_request.get_job_param("action")
    module = job_request.get_job_param("module") or "Contracts"
    try:
        run(record_id, action)
        context.close_with_success()
    except Exception as exc:  # noqa: BLE001 -- tell the person on the record, then fail the job
        log.exception("contract ingest failed")
        try:
            if action not in NEW_FROM_PACKET:
                zc.update_record("Contracts", record_id, {"Ingest_Status": "Failed"})
            zc.post_note(module, record_id, "Contract ingest", "Ingest failed: {}".format(str(exc)[:1500]))
        except Exception:  # noqa: BLE001
            log.exception("could not post the failure note")
        context.close_with_failure()


COUNTERPARTY_TYPES = ("Customer", "Vendor")
NO_TYPE_MSG = ("Counterparty_Type is blank on this contract, so the ingest did not run -- vendor and customer "
               "packets are read with different rules. Set Counterparty_Type to Vendor (Allied, SCRAM, SuperCom, "
               "Buddi) or Customer (everyone else) and press the button again.")

# action -> (parent module, parent name field, parent lookup on Contracts, Counterparty_Type)
NEW_FROM_PACKET = {
    "new_contract_from_packet": ("Accounts", "Account_Name", "Account", "Customer"),
    "new_vendor_contract_from_packet": ("Vendors", "Vendor_Name", "Vendor", "Vendor"),
}


def run(record_id, action):
    if action in NEW_FROM_PACKET:
        contract_id = _create_contract_from_parent(record_id, *NEW_FROM_PACKET[action])
    else:
        contract_id = record_id
    _ingest_contract(contract_id)


def _create_contract_from_parent(parent_id, module, name_field, lookup_field, counterparty_type):
    parent = zc.get_record(module, parent_id, fields=[name_field])
    suffix = "vendor pricing from packet" if counterparty_type == "Vendor" else "from packet"
    contract = zc.create_record("Contracts", {
        "Name": "{} - {}".format(parent.get(name_field) or "New contract", suffix)[:120],  # mandatory
        lookup_field: {"id": parent_id},
        "Contract_Status": "In Progress",  # "Draft" is not a Contract_Status value
        "Ingest_Status": "Not Ingested",
        "Counterparty_Type": counterparty_type,
    })
    contract_id = contract["details"]["id"]
    # Move the zip so the packet lives in exactly one place (on the Contract).
    for att in zc.list_attachments(module, parent_id):
        name = att.get("File_Name", "")
        if name.lower().endswith(".zip"):
            raw = zc.download_attachment(module, parent_id, att["id"])
            zc.upload_attachment("Contracts", contract_id, name, raw)
            zc.delete_attachment(module, parent_id, att["id"])
    return contract_id


def _ingest_contract(contract_id):
    contract = zc.get_record("Contracts", contract_id, fields=CONTRACT_FIELDS)
    stored_hash = contract.get("Packet_Hash")
    counterparty_type = contract.get("Counterparty_Type")
    if counterparty_type not in COUNTERPARTY_TYPES:
        zc.update_record("Contracts", contract_id, {"Ingest_Status": "Failed"})
        zc.post_note("Contracts", contract_id, "Contract ingest", NO_TYPE_MSG)
        return

    attachments = zc.list_attachments("Contracts", contract_id)
    # 20 Oct 2026: take the NEWEST zip. The old code took whichever the API listed first, so a stale packet
    # from an earlier attempt could be read instead of the one just attached.
    zips = [a for a in attachments if a.get("File_Name", "").lower().endswith(".zip")]
    zip_attachment = max(zips, key=lambda a: a.get("Created_Time") or "") if zips else None
    if zip_attachment:
        packet_name = zip_attachment.get("File_Name", "packet.zip")
        zip_bytes = zc.download_attachment("Contracts", contract_id, zip_attachment["id"])
    else:
        # No zip: a single signed PDF (or a few loose files) is a packet too -- bundle every readable
        # attachment into an in-memory zip so the rest of the pipeline is unchanged.
        loose = sorted((a for a in attachments if _readable(a.get("File_Name", ""))), key=lambda a: a["File_Name"])
        if not loose:
            zc.update_record("Contracts", contract_id, {"Ingest_Status": "Failed"})
            zc.post_note("Contracts", contract_id, "Contract ingest", NO_ATTACHMENTS_MSG)
            return
        packet_name = loose[0]["File_Name"] if len(loose) == 1 else "attachments ({} files)".format(len(loose))
        zip_bytes = bundle_files([(a["File_Name"], zc.download_attachment("Contracts", contract_id, a["id"]))
                                  for a in loose])
    entries, packet_sha256, warnings = read_packet(zip_bytes)

    if stored_hash and stored_hash == packet_sha256:
        zc.post_note("Contracts", contract_id, "Contract ingest",
                     "{} already read earlier (Packet_Hash unchanged) -- nothing new to ingest.".format(packet_name))
        return

    read_lines, skipped_lines, no_text_layer_lines = [], [], []
    docs, entry_by_name = [], {}
    for e in entries:
        if e.skip_reason:
            skipped_lines.append(e.to_summary())
            continue
        if e.no_text_layer:
            no_text_layer_lines.append(e.name)
            continue  # nothing to extract; indexed below with Text_Layer = No
        docs.append(PacketDoc(name=e.name, text=e.text))
        entry_by_name[e.name] = e

    result = extract_packet(docs, counterparty_type,
                            contract_has_account=bool(contract.get("Account")), packet_name=packet_name)

    # ---- Contract_Documents index (Phase 2 behaviour, now with better dates and Applies_To) ----
    for seq, d in enumerate(result.docs, start=1):
        e = entry_by_name[d.name]
        _upsert_contract_document(contract_id, d.name, e.sha1, d.doc_type, e.pages, d.effective_date, "Yes",
                                  extract_applies_to_contracts(d.text) if d.doc_type == "Addendum" else "", seq,
                                  parse_status="Parsed")
        read_lines.append("{} -> {}{}".format(d.name, d.doc_type,
                                              ", eff. {} ({})".format(d.effective_date, d.date_rule) if d.effective_date else ""))
    for name in no_text_layer_lines:
        e = next(x for x in entries if x.name == name)
        _upsert_contract_document(contract_id, name, e.sha1, classify(name, ""), e.pages, None, "No", "", 0,
                                  parse_status="No text layer")

    # ---- Phase 3b: rate lines ----
    existing_rows = contract.get("Rate_Lines") or []
    rate_plan, rate_notes = plan_rate_lines(existing_rows, result.rate_rows, result.superseded,
                                            counterparty_type)
    _apply_rate_plan(contract_id, existing_rows, rate_plan)

    # ---- Phase 3c: term + pricing fields ----
    decisions = review_payload.decisions_of(contract.get("Ingest_Review"))
    field_updates, field_notes = plan_contract_fields(
        contract, result.terms, result.pricing, [(d.doc_type, d.effective_date) for d in result.docs],
        decisions=decisions)
    # Phase 4: a field a person decided in the Contract Review widget is never rewritten by a rerun.
    field_updates, review_notes = review_payload.apply_decisions(field_updates, decisions)
    field_notes = field_notes + review_notes
    # The computed end dates, shown in the review for Confirm / Edit (same inputs the plan used).
    derived_dates, _ = derive_term_dates(make_eff(contract, field_updates, decisions))

    # ---- Phase 3d: clauses ----
    clause_props, clause_dropped, clause_notes = find_clauses(
        result.docs, counterparty_type, packet_name,
        start_date=field_updates.get("Contract_Start_Date") or contract.get("Contract_Start_Date"),
        end_date=field_updates.get("Contract_End_Date") or contract.get("Contract_End_Date"))
    clause_created = 0
    if clause_props:
        existing_clauses = zc.coql(
            "select id, Clause_Hash, Status from Contract_Clauses where Contract = '{}' limit 200".format(contract_id))
        account = contract.get("Account") or {}
        clause_plan, more = plan_clauses(existing_clauses, clause_props, contract_id, account.get("id"))
        clause_notes.extend(more)
        if WRITE_CLAUSES:
            for fields in clause_plan["create"]:
                try:
                    zc.create_record("Contract_Clauses", fields)
                    clause_created += 1
                except Exception as exc:  # one bad row must not lose the rates/terms already written
                    clause_notes.append("could not create clause '{}': {}".format(fields.get("Title"), exc))
        else:
            clause_notes.append("clause write-back is off (WRITE_CLAUSES) -- {} row(s) would be created".format(
                len(clause_plan["create"])))

    readable_count = len(entries) - len(skipped_lines)
    all_no_text = readable_count > 0 and len(no_text_layer_lines) == readable_count
    ingest_status = "Failed" if (readable_count == 0 or all_no_text) else "Needs Review"

    summary = _build_summary(read_lines, skipped_lines, no_text_layer_lines, warnings, result,
                             rate_plan, rate_notes, field_updates, field_notes,
                             clause_props, clause_dropped, clause_created, clause_notes)
    summary = "Read as a {} contract (Counterparty_Type).\n".format(counterparty_type) + summary
    update = dict(field_updates)
    update.update({
        "Ingest_Status": ingest_status if contract.get("Ingest_Status") != "Ingested" else "Ingested",
        "Ingested_On": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S-00:00"),
        "Packet_Hash": packet_sha256,
        "Ingest_Summary": summary[:2000],
        "Ingest_Review": review_payload.build(contract.get("Ingest_Review"), result, field_updates, packet_name,
                                              is_current=is_current, derived=derived_dates),
    })
    zc.update_record("Contracts", contract_id, update)
    zc.post_note("Contracts", contract_id, "Contract ingest", summary[:NOTE_LIMIT])


def _readable(name):
    return ("." + name.rsplit(".", 1)[-1].lower()) in READABLE_EXT if "." in name else False


def bundle_files(files):
    """[(name, bytes)] -> zip bytes. Fixed timestamps so the same files always give the same Packet_Hash
    (an unchanged PDF is then "already read earlier", like an unchanged zip)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, raw in files:
            zf.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), raw)
    return buf.getvalue()


def _apply_rate_plan(contract_id, existing_rows, plan):
    """Rate_Lines is a subform: a PUT replaces the whole list, and any existing row whose id is left out
    is DELETED by Zoho. So every existing row id is always sent back, changed or not."""
    if not (plan["update"] or plan["insert"] or plan["supersede"]):
        return
    payload = []
    for r in existing_rows:
        item = {"id": r["id"]}
        item.update(plan["update"].get(r["id"], {}))
        payload.append(item)
    payload.extend(fields for _, fields in plan["insert"])
    zc.update_record("Contracts", contract_id, {"Rate_Lines": payload})

    if not plan["supersede"]:
        return
    # Second pass: find the ids Zoho gave the inserted rows, then set Superseded_By.
    fresh = zc.get_record("Contracts", contract_id, fields=["Rate_Lines"]).get("Rate_Lines") or []
    old_ids = {r["id"] for r in existing_rows}
    new_ids = {}
    for (ref, fields) in plan["insert"]:
        for r in fresh:
            if r["id"] in old_ids or r["id"] in new_ids.values():
                continue
            if (r.get("Rate_Device") == fields["Rate_Device"] and (r.get("Tier_Min_Units") or 0) == (fields["Tier_Min_Units"] or 0)
                    and r.get("Effective_Date") == fields["Effective_Date"]
                    and abs(float(r.get("Rate_Daily_Amount") or 0) - fields["Rate_Daily_Amount"]) < 0.005):
                new_ids[ref] = r["id"]
                break

    def resolve(ref):
        return ref[1] if ref[0] == "id" else new_ids.get(ref)

    sup = {}
    for old_ref, new_ref in plan["supersede"]:
        o, n = resolve(old_ref), resolve(new_ref)
        if o and n:
            sup[o] = n
    if sup:
        zc.update_record("Contracts", contract_id, {"Rate_Lines": [
            dict({"id": r["id"]}, **({"Superseded_By": sup[r["id"]]} if r["id"] in sup else {})) for r in fresh]})


def _upsert_contract_document(contract_id, file_name, sha1, doc_type, pages, effective_date, text_layer,
                              applies_to, sequence, parse_status=None):
    # Key = parent + File_Name + sha1. File_Name is matched here in Python, not in the COQL: COQL rejects
    # a backslash-escaped quote (SYNTAX_ERROR at the start of the where clause), so a file name with an
    # apostrophe ("Sheriff's addendum.pdf") broke the whole ingest (28 Sep 2026). contract_id is digits and
    # sha1 is hex, so this query can never hold a quote.
    rows = zc.coql(
        "select id, File_Name from Contract_Documents where (Contract = '{}' and Sha1 = '{}') limit 200"
        .format(contract_id, sha1)
    )
    existing = [r for r in rows if (r.get("File_Name") or "") == file_name]
    data = {
        "Name": file_name[:120],  # mandatory record-name field on Contract_Documents
        "Contract": {"id": contract_id},
        "File_Name": file_name,
        "Doc_Type": doc_type,
        "Sequence": sequence,
        "Effective_Date": effective_date,
        "Pages": pages,
        "Text_Layer": text_layer,
        "Applies_To_Contracts": applies_to,
        "Sha1": sha1,
    }
    if parse_status:
        # 12 Oct 2026: the Contract Ingest widget polls this field to show a chip per file. Picklist values:
        # Parsed, No text layer, Skipped, Failed. Skipped files are not indexed, so only the first two are
        # written here; a whole-run failure is already reported through Ingest_Status = Failed.
        data["Parse_Status"] = parse_status
    if existing:
        zc.update_record("Contract_Documents", existing[0]["id"], data)
    else:
        zc.create_record("Contract_Documents", data)


def _build_summary(read_lines, skipped_lines, no_text_layer_lines, warnings, result, rate_plan, rate_notes,
                   field_updates, field_notes, clause_props=(), clause_dropped=0, clause_created=0, clause_notes=()):
    parts = ["Read {} file(s), in effective-date order: {}".format(
        len(read_lines), "; ".join(read_lines) if read_lines else "none")]
    if skipped_lines:
        parts.append("Skipped {}: {}".format(len(skipped_lines), "; ".join(skipped_lines)))
    if no_text_layer_lines:
        parts.append("{} file(s) have no text layer and were not read -- OCR on Beau's PC and re-upload: {}".format(
            len(no_text_layer_lines), ", ".join(no_text_layer_lines)))
    if warnings:
        parts.append("Warnings: " + "; ".join(warnings))

    parts.append("RATES: {} inserted, {} updated/cited, {} superseded.".format(
        len(rate_plan["insert"]), len(rate_plan["update"]), len(rate_plan["supersede"])))
    for r in result.rate_rows:
        parts.append("  {} {}{} ${:.2f}/day eff {} [{}] {}".format(
            r.device, "tier {}{} ".format(r.tier_min, "-{}".format(r.tier_max) if r.tier_max else "+") if r.tier_min else "",
            "(purchased) " if r.ownership else "", r.amount, r.effective_date or "?", r.confidence, r.source))
    parts.extend("  " + n for n in rate_notes + result.notes)

    parts.append("TERMS: {} field(s) written: {}".format(
        len(field_updates), ", ".join("{}={}".format(k, v) for k, v in field_updates.items()) or "none"))
    for f in result.terms.values():
        if f.field != "Escalator_Note":
            parts.append("  " + f.as_note())
    parts.extend("  " + n for n in field_notes)
    parts.append("CLAUSES: {} proposed ({} created as Contract_Clauses rows), {} candidate sentence(s) dropped as "
                 "boilerplate, duplicates or already-captured fields.".format(len(clause_props), clause_created, clause_dropped))
    for p in clause_props:
        parts.append("  " + p.as_note())
    parts.extend("  " + n for n in clause_notes)
    parts.append("Nothing above is marked verified -- review the lines and clauses, set Line_Verified, "
                 "Confirm/Reject each clause, and set Ingest_Status = Ingested when correct.")
    return "\n".join(parts)

"""Ingest Requirements: rule-based, no AI tokens (Beau, Sep 25 2026).

One press reads every attachment on the Solicitation that has not been read yet and
writes DRAFT Solicitation_Requirements rows (Our_Position = Unverified) for a person
to verify. Nothing is sent to any model.

  PDF   text layer per page -> shall/must sentences, numbered sections, PDF page numbers
  XLSX  requirement matrices (Form K) -> one row per table line, header found by rule
  DOCX / TXT / CSV -> same sentence rules as PDF, no page numbers
  ZIP   every supported file inside is read; rows cite "archive.zip > file.pdf"
  Scanned PDFs have no text layer: marked MANUAL, never shown as complete.

Progress is kept in the Solicitation's Ingest_Queue, one line per attachment:

    <attachmentId>|<pages or rows>|DONE|MANUAL|SKIP|FAILED|<file name - note>

Clearing an attachment id from Ingested_Attachments re-reads that document; dedupe
is on normalised requirement text across the whole Solicitation, so a re-read
creates nothing new unless the file changed.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from . import briefdoc, briefing
from .crm import Crm, CrmError
from .extract import expand_zip, kind_of, parse_document, to_document
from .rule_ingest import ENGINE

CATEGORIES = ["Insurance", "Bonding", "Licensure", "Staffing", "Experience / Refs", "Technology / Device",
              "Security / Encryption", "Reporting", "Financial / Audit", "Legal / Terms", "Performance / SLA",
              "DBE / MBE", "Submission format", "Other"]
PRIORITIES = ["Core", "High", "Medium", "Low", "Unspecified"]
TEXT_LIMITS = {"Name": 120, "Section_Group": 120, "Source_Reference": 120, "Page_PDF": 30}
FINISHED = ("DONE", "MANUAL", "SKIP", "FAILED")
LOCK_PREFIX = "#RUNNING "
LOCK_STALE_S = 20 * 60
FILE_START_MARGIN = 60  # never start a new document with less than this left


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _stamp() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Chicago")).replace(microsecond=0).isoformat()
    except Exception:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------- queue ----------------

def parse_queue(text: str | None) -> list[list[str]]:
    lines = []
    for raw in (text or "").replace("\r", "").split("\n"):
        parts = raw.split("|", 3)
        if len(parts) == 4 and parts[0].strip() and not raw.startswith("#"):
            lines.append([p.strip() for p in parts])
    return lines


def render_queue(lines: list[list[str]]) -> str:
    return "\n".join("|".join(l) for l in lines)


def _lock_age(queue_text: str | None) -> float | None:
    for raw in (queue_text or "").split("\n"):
        if raw.startswith(LOCK_PREFIX):
            try:
                return time.time() - float(raw[len(LOCK_PREFIX):].split()[0])
            except (ValueError, IndexError):
                return None
    return None


# ---------------- records ----------------

def _cut(field: str, v):
    if v is None:
        return None
    lim = TEXT_LIMITS.get(field)
    return v[:lim] if lim else v


def to_rows(reqs: list[dict], sol_id: str, sol_lk: str, file_label: str, existing_texts: set[str],
            existing_names: set[str], allowed: set[str] | None = None) -> tuple[list[dict], int]:
    """reqs: rule_ingest rows. Returns (CRM rows, duplicates skipped)."""
    rows, dupes = [], 0
    for r in reqs:
        text = (r.get("Requirement_Text") or "").strip()
        key = _norm(text)
        if not key or key in existing_texts:
            dupes += 1
            continue
        existing_texts.add(key)
        ref, page = r.get("Source_Reference"), r.get("Page_PDF")
        name = (ref or " ".join(text.split()[:8]))[:110]
        base, i = name, 2
        while name.lower() in existing_names:
            name = f"{base} ({i})"
            i += 1
        existing_names.add(name.lower())
        where = ", ".join(x for x in (file_label, ref and ("§" + ref if ref[0].isdigit() else ref),
                                      page and f"p.{page}") if x)
        row = {
            "Name": name,
            sol_lk: {"id": sol_id},
            "Requirement_Text": text[:31000],
            "Section_Group": _cut("Section_Group", r.get("Section_Group") or file_label),
            "Category": r.get("Category") if r.get("Category") in CATEGORIES else "Other",
            "Priority": r.get("Priority") if r.get("Priority") in PRIORITIES else "Unspecified",
            "Mandatory": bool(r.get("Mandatory")),
            "Source_Reference": _cut("Source_Reference", where),
            "Our_Position": "Unverified",
        }
        if page:
            row["Page_PDF"] = _cut("Page_PDF", str(page))
        if allowed is not None:
            row = {k: v for k, v in row.items() if k in allowed or k == sol_lk}
        rows.append(row)
    return rows, dupes


def _extent(doc: dict) -> str:
    if doc.get("kind") == "pdf":
        return f"{len(doc['pages'])} pages"
    if doc.get("kind") == "xlsx":
        return f"{sum(len(g) for _, g in doc['sheets'])} rows"
    return "text"


def run(crm: Crm, sol_id: str, budget_s: float = 840, dry_run: bool = False) -> str:
    t0 = time.time()
    deadline = t0 + budget_s
    sol = crm.get_record("Solicitations", sol_id)
    age = _lock_age(sol.get("Ingest_Queue"))
    if age is not None and age < LOCK_STALE_S:
        return (f"An ingest run started {age / 60:.0f} minute(s) ago is still working on this Solicitation. "
                "Wait for its Note, then press again if it says documents remain.")
    sol_lk = crm.lookup_field_to("Solicitation_Requirements", "Solicitations")
    try:
        allowed = {f["api_name"] for f in crm.fields("Solicitation_Requirements")} or None
    except (CrmError, AttributeError, KeyError, TypeError):
        allowed = None

    queue = parse_queue(sol.get("Ingest_Queue"))
    done_ids = {x.strip() for x in (sol.get("Ingested_Attachments") or "").split("\n") if x.strip()}
    # an attachment cleared from Ingested_Attachments is a request to re-read it
    queue = [l for l in queue if l[0] in done_ids]

    atts = crm.attachments("Solicitations", sol_id)
    names = {a["id"]: a.get("File_Name") or a["id"] for a in atts}
    skipped_files: list[str] = []
    todo = []
    for a in atts:
        if a["id"] in done_ids:
            continue
        if names[a["id"]].startswith(briefdoc.BRIEFING_PREFIX):
            continue  # our own briefing document is output, never a source
        if kind_of(names[a["id"]]) is None:
            skipped_files.append(names[a["id"]])
            continue
        todo.append(a["id"])

    try:
        existing = crm.coql("select Name, Requirement_Text, Mandatory, Category, Source_Reference, Page_PDF "
                            f"from Solicitation_Requirements where {sol_lk} = {int(sol_id)}")
    except CrmError:   # a column the briefing summary wants is missing: fall back to the plain read
        existing = crm.coql(f"select Name, Requirement_Text from Solicitation_Requirements where {sol_lk} = {int(sol_id)}")
    req_rows = list(existing)
    found = briefing.new_findings()
    texts = {_norm(e.get("Requirement_Text") or "") for e in existing}
    rnames = {(e.get("Name") or "").lower() for e in existing}

    created = dupes = files_done = 0
    manual: list[str] = []
    notes: list[str] = []
    lock = f"{LOCK_PREFIX}{time.time():.0f} (started {_stamp()})"

    def save_queue(extra: dict | None = None, locked: bool = True):
        text = render_queue(queue)
        if locked:
            text = lock + ("\n" + text if text else "")
        upd = {"Ingest_Queue": text[-31000:]}
        upd.update(extra or {})
        if not dry_run:
            crm.update("Solicitations", sol_id, upd, trigger=())

    remaining = len(todo)
    try:
        save_queue()  # take the lock
        for aid in todo:
            if time.time() > deadline - FILE_START_MARGIN:
                break
            fname = names[aid].replace("|", "/")
            try:
                data = crm.download_attachment("Solicitations", sol_id, aid)
                if kind_of(fname) == "zip":
                    members, zskip = expand_zip(data)
                    parts = [(f"{fname} > {m.rsplit('/', 1)[-1]}".replace("|", "/"), b) for m, b in members]
                    skipped_files.extend(f"{fname} > {x}" for x in zskip)
                    if not parts:
                        skipped_files.append(f"{fname} (no readable files inside)")
                else:
                    parts, zskip = [(fname, data)], []
                new_n = dup_n = 0
                man_here, skip_here = [], list(zskip)
                extent = "-"
                for label, blob in parts:
                    doc = to_document(label, blob)
                    if "skip" in doc:
                        skipped_files.append(f"{label} ({doc['skip']})")
                        skip_here.append(label)
                        continue
                    try:   # the briefing is a bonus read: a problem here never blocks the requirements
                        briefing.merge(found, briefing.scan(label, doc))
                    except Exception as e:  # noqa: BLE001
                        notes.append(f"Briefing read of {label}: {str(e)[:120]}")
                    res = parse_document(doc)
                    rows, d = to_rows(res["rows"], sol_id, sol_lk, label, texts, rnames, allowed)
                    if rows and not dry_run:
                        crm.create_many("Solicitation_Requirements", rows, trigger=())
                    created += len(rows)
                    req_rows.extend(rows)
                    dupes += d
                    new_n += len(rows)
                    dup_n += d
                    extent = _extent(doc) if len(parts) == 1 else f"{len(parts)} files"
                    if res["needs_manual"]:
                        manual.append(label)
                        man_here.append(label.rsplit(" > ", 1)[-1])
                if len(parts) == 1 and skip_here and not man_here and new_n == 0 and dup_n == 0:
                    queue.append([aid, "-", "SKIP", f"{fname} - {skipped_files[-1]}"[:300]])
                elif len(parts) == 1 and man_here:
                    queue.append([aid, extent, "MANUAL", f"{fname} - no text layer, enter by hand"])
                else:
                    note = f"{fname} - {new_n} new, {dup_n} duplicate"
                    if man_here:
                        note += f"; by hand: {', '.join(man_here[:5])}"
                    if skip_here and (len(parts) > 1 or zskip):
                        note += f"; not read: {len(skip_here)}"
                    queue.append([aid, extent if parts else "0 files", "DONE", note[:300]])
                files_done += 1
                done_ids.add(aid)
            except Exception as e:  # noqa: BLE001 - one bad file must not block the others
                notes.append(f"{fname}: {str(e)[:200]}")
                queue.append([aid, "-", "FAILED", f"{fname} - {str(e)[:150]}"])
                done_ids.add(aid)
            remaining -= 1
            try:
                save_queue()
            except CrmError as e:
                notes.append(f"Queue save failed: {str(e)[:200]}")
    finally:
        elapsed = time.time() - t0
        summary = (f"{created} draft requirement(s) created, {dupes} duplicate(s) skipped, {files_done} document(s) "
                   f"read by rule in {elapsed:.0f}s (no AI).")
        if not atts:
            summary = ("No attachments found on this record. Attach the solicitation files (or one .zip of them) "
                       "in the Attachments section, then press Ingest Requirements again.")
        elif remaining:
            summary += f" {remaining} document(s) still to go - press Ingest Requirements again."
        elif not todo:
            summary += " Every attachment was already read earlier; to re-read one, clear its id from Ingested Attachments."
        else:
            summary += " All attached documents are read."
        if manual:
            summary += (" No text layer (likely scanned) - enter requirements by hand: "
                        + "; ".join(manual[:8]) + ".")
        if skipped_files:
            summary += " Not read: " + "; ".join(skipped_files[:8]) + "."
        if created:
            summary += " Every row is Unverified: check wording, Mandatory, Category and page before the team relies on them."
        brief_upd: dict = {}
        try:
            brief_upd, brief_notes = briefing.fields_for(sol, found, req_rows)
            notes.extend(brief_notes)
        except Exception as e:  # noqa: BLE001
            notes.append(f"Briefing: {str(e)[:150]}")
        if brief_upd:
            names_ = [k.replace("_", " ") for k in brief_upd if k not in ("Briefing_Status", "Header_Sources", "Budget_Disclosed",
                                                                          "Eval_Weights_Published", "Deadline_Timezone_Note")]
            summary += (" Briefing drafted (empty fields only, nothing overwritten): " + ", ".join(names_[:12])
                        + ". Check the dates and the quoted sources before briefing leadership.")
        line_ = f"{_stamp()[:16].replace('T', ' ')} Rule ingest: {summary}"
        if notes:
            line_ += " Errors: " + " | ".join(notes[:5])

        extra: dict = {**brief_upd, "Ingested_Attachments": "\n".join(sorted(done_ids)),
                       "Extraction_Notes": ((sol.get("Extraction_Notes") or "") + "\n" + line_).strip()[-31000:]}
        for fname, val in (("Parse_Date", _stamp()), ("Key_Dates_Verified", False),
                           ("Parsed_By", f"Solicitation pipeline (Python / {ENGINE})")):
            try:
                f = crm.field("Solicitations", fname)
            except CrmError:
                f = None
            if f and (fname != "Parsed_By" or f.get("data_type") in ("text", "textarea")):
                extra[fname] = val
        save_queue(extra, locked=False)
    return ("[dry run] " if dry_run else "") + summary

"""Build the executive / RFP team briefing document (.docx) for one Solicitation.

No AI. The document is laid out from fields already on the record (drafted by the
ingest, then corrected by people) plus the requirement rows, so it always shows
what the team has agreed, not a second opinion. Standard library only.

Press "Build Briefing" -> a Word file named "Briefing - <name> - <timestamp>.docx"
is attached to the Solicitation. Ingest skips any attachment whose name starts
with BRIEFING_PREFIX, so the document is never read back in as a requirement source.
"""
from __future__ import annotations

import io
import time
import zipfile
from datetime import datetime
from xml.sax.saxutils import escape

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from . import evalfacts, services
from .crm import Crm, CrmError

BRIEFING_PREFIX = "Briefing - "
ENGINE = "briefdoc-2026-10-16.1"
MAX_REQ_ROWS = 40

DATE_ROWS = [
    ("Release_Date", "Released"),
    ("Questions_Round_1_Due", "Questions round 1 due"),
    ("Questions_Round_2_Due", "Questions round 2 due"),
    ("Pre_Proposal_Conference", "Pre-proposal conference"),
    ("Partner_Commitment_Due", "Partner commitment due"),
    ("Teaming_LOI_Due", "Teaming LOI due"),
    ("Internal_Upload_Target", "Internal upload target"),
    ("Proposal_Due", "PROPOSAL DUE"),
    ("Field_Test_Start", "Field test starts"),
    ("Field_Test_End", "Field test ends"),
    ("Shortlist_Window_Start", "Shortlist window starts"),
    ("Shortlist_Window_End", "Shortlist window ends"),
    ("Anticipated_Award", "Anticipated award"),
    ("Anticipated_Start", "Anticipated start"),
]


# a contract renewal notice answers to the agency, not to a proposal deadline
RENEWAL_LABELS = {"Proposal_Due": "RENEWAL RESPONSE DUE", "Anticipated_Start": "Renewal term starts",
                  "Release_Date": "Notice date"}


def _blank(v) -> bool:
    return v in (None, "", "-None-", [], {})


def _name(v) -> str:
    if isinstance(v, dict):
        return str(v.get("name") or v.get("display_value") or "")
    if isinstance(v, list):
        return ", ".join(_name(x) for x in v)
    return "" if v is None else str(v)


def fmt_date(v, tzname: str = "America/Chicago") -> str:
    """CRM date ('2026-10-20') or datetime ('2026-10-20T12:00:00-05:00')."""
    if _blank(v):
        return ""
    s = str(v)
    try:
        if "T" not in s:
            return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%a %b %d, %Y").replace(" 0", " ")
        dt = datetime.fromisoformat(s)
        if ZoneInfo and dt.tzinfo:
            dt = dt.astimezone(ZoneInfo(tzname))
        t = dt.strftime("%I:%M %p").lstrip("0")
        return dt.strftime("%a %b %d, %Y").replace(" 0", " ") + f" {t} Central"
    except ValueError:
        return s


def fmt_money(v) -> str:
    try:
        return "${:,.0f}".format(float(v))
    except (TypeError, ValueError):
        return _name(v)


# ---------- WordprocessingML helpers ----------
def _run(text: str, bold=False, italic=False, size=None, color=None) -> str:
    pr = ""
    if bold:
        pr += "<w:b/>"
    if italic:
        pr += "<w:i/>"
    if color:
        pr += f'<w:color w:val="{color}"/>'
    if size:
        pr += f'<w:sz w:val="{size}"/>'
    parts = str(text).split("\n")
    xml = []
    for i, p in enumerate(parts):
        if i:
            xml.append("<w:br/>")
        xml.append(f'<w:t xml:space="preserve">{escape(p)}</w:t>')
    return f"<w:r>{'<w:rPr>' + pr + '</w:rPr>' if pr else ''}{''.join(xml)}</w:r>"


def para(text="", style=None, **kw) -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{ppr}{_run(text, **kw) if text != '' else ''}</w:p>"


def text_blocks(text: str) -> list[str]:
    """One paragraph per non-empty line; keeps '-' / '*' lines as bullets."""
    out = []
    for line in str(text).splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith(("- ", "* ", "• ")):
            out.append(para("• " + line.lstrip()[2:].strip()))
        else:
            out.append(para(line.strip()))
    return out


def table(rows: list[list[str]], widths: list[int], header=True) -> str:
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    out = [f'<w:tbl><w:tblPr><w:tblW w:w="{sum(widths)}" w:type="dxa"/>'
           '<w:tblBorders>' + "".join(f'<w:{s} w:val="single" w:sz="4" w:color="BBBBBB"/>'
                                      for s in ("top", "left", "bottom", "right", "insideH", "insideV")) +
           f'</w:tblBorders></w:tblPr><w:tblGrid>{grid}</w:tblGrid>']
    for ri, row in enumerate(rows):
        cells = []
        for ci, cell in enumerate(row):
            hd = header and ri == 0
            shade = '<w:shd w:val="clear" w:color="auto" w:fill="E7EEF5"/>' if hd else ""
            body = "".join(para(x, bold=hd or (ci == 0 and not header)) for x in (str(cell).split("\n") or [""]))
            cells.append(f'<w:tc><w:tcPr><w:tcW w:w="{widths[ci]}" w:type="dxa"/>{shade}</w:tcPr>{body}</w:tc>')
        out.append("<w:tr>" + "".join(cells) + "</w:tr>")
    out.append("</w:tbl>")
    return "".join(out) + para()


def _styles() -> str:
    def st(sid, name, sz, bold=True, color="1F3A5F", before=240, after=80):
        return (f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/><w:basedOn w:val="Normal"/>'
                f'<w:pPr><w:keepNext/><w:spacing w:before="{before}" w:after="{after}"/></w:pPr>'
                f'<w:rPr>{"<w:b/>" if bold else ""}<w:color w:val="{color}"/><w:sz w:val="{sz}"/></w:rPr></w:style>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
            '<w:sz w:val="21"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="80"/></w:pPr></w:pPrDefault>'
            '</w:docDefaults><w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
            + st("Title", "Title", 40, before=0, after=60)
            + st("Heading1", "heading 1", 28)
            + st("Heading2", "heading 2", 23, color="3B3B3B")
            + '</w:styles>')


def pack(body_xml: str) -> bytes:
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           + body_xml +
           '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
           '<w:pgMar w:top="1080" w:right="1080" w:bottom="1080" w:left="1080" w:header="720" w:footer="720" w:gutter="0"/>'
           '</w:sectPr></w:body></w:document>')
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
          '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
          '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
    drels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
             '</Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", drels)
        z.writestr("word/styles.xml", _styles())
    return buf.getvalue()


# ---------- content ----------
def _section(title: str, text) -> list[str]:
    out = [para(title, "Heading1")]
    if _blank(text):
        out.append(para("Nothing recorded yet.", italic=True, color="888888"))
    else:
        out.extend(text_blocks(text))
    return out


def build(sol: dict, reqs: list[dict], now: str | None = None) -> bytes:
    now = now or time.strftime("%b %d, %Y %H:%M UTC", time.gmtime())
    b: list[str] = []
    b.append(para(sol.get("Name") or "Solicitation briefing", "Title"))
    meta = [("Solicitation number", sol.get("Solicitation_Number")),
            ("Issuing agency", _name(sol.get("Issuing_Agency")) or sol.get("Issuing_Agency_Text")),
            ("Type / contract", " / ".join(x for x in (sol.get("Solicitation_Type"), sol.get("Contract_Type")) if x)),
            ("Service lines", _name(sol.get("Service_Lines"))),
            ("Status", sol.get("Status")),
            ("Briefing status", sol.get("Briefing_Status")),
            ("Briefed on", fmt_date(sol.get("Briefed_On"))),
            ("Portal", sol.get("Portal_URL") or sol.get("Portal"))]
    b.append(table([[k, str(v)] for k, v in meta if not _blank(v)], [2600, 8300], header=False))

    renewal = sol.get("Solicitation_Type") == "Renewal Notice"
    if renewal:
        b += renewal_blocks(sol)

    verified = bool(sol.get("Key_Dates_Verified"))
    b.append(para("Key dates", "Heading1"))
    if not verified:
        b.append(para("DRAFT: dates were read from the packet by rule and have not been verified by a person. "
                      "A time shown as 12:00 with no zone in the packet is a placeholder (Central assumed).",
                      bold=True, color="B45309"))
    rows = [["Milestone", "When"]]
    for api, label in DATE_ROWS:
        v = sol.get(api)
        if renewal:
            label = RENEWAL_LABELS.get(api, label)
        if not _blank(v):
            extra = ""
            if api == "Pre_Proposal_Conference" and sol.get("Pre_Proposal_Mandatory"):
                extra = "  (MANDATORY)"
            if api == "Proposal_Due" and sol.get("Deadline_Timezone_Note"):
                extra = f"  ({sol['Deadline_Timezone_Note']})"
            rows.append([label, fmt_date(v) + extra])
    if len(rows) > 1:
        b.append(table(rows, [3600, 7300]))
    else:
        b.append(para("No dates recorded yet.", italic=True, color="888888"))

    b += _section("Scope of work", services.split_renewal_block(sol.get("Scope_of_Work"))[1])
    b += services_blocks(sol, reqs)

    b.append(para("Evaluation and budget", "Heading1"))
    money = []
    if sol.get("Budget_Disclosed") or not _blank(sol.get("Stated_Budget")):
        money.append(["Stated budget", fmt_money(sol.get("Stated_Budget")) if not _blank(sol.get("Stated_Budget"))
                      else "Disclosed, amount not recorded"])
    else:
        money.append(["Stated budget", "Not disclosed"])
    for api, label in (("Pricing_Model", "Pricing model"), ("Reimbursement_Structure", "Reimbursement"),
                       ("Contract_Term", "Contract term"), ("Offer_Validity_Days", "Offer valid (days)"),
                       ("Bond_Required", "Bond required"), ("Insurance_Requirements", "Insurance"),
                       ("Incumbent", "Incumbent"), ("Known_Competitors", "Known competitors")):
        if not _blank(sol.get(api)):
            money.append([label, str(sol[api])])
    if sol.get("Eval_Weights_Published"):
        money.append(["Evaluation weights", "Published in the solicitation"])
    b.append(table(money, [3000, 7900], header=False))
    b += evaluation_blocks(sol)
    if not _blank(sol.get("Solicitation_Defects")):
        b.append(para("Defects and ambiguities noted", "Heading2"))
        b += text_blocks(sol["Solicitation_Defects"])

    b += units_blocks(sol, reqs)
    b += _section("Key requirements", sol.get("Key_Requirements_Summary"))
    b += _section("Submission requirements", sol.get("Submission_Requirements"))

    mand = [r for r in reqs if r.get("Mandatory")]
    b.append(para(f"Requirements on file ({len(reqs)} total, {len(mand)} mandatory)", "Heading2"))
    if mand:
        rows = [["Requirement", "Category", "Source", "Our position"]]
        for r in mand[:MAX_REQ_ROWS]:
            ref = ", ".join(x for x in (r.get("Source_Reference"),
                                        f"p.{r['Page_PDF']}" if r.get("Page_PDF") else None) if x)
            rows.append([(r.get("Requirement_Text") or r.get("Name") or "")[:400], r.get("Category") or "",
                         ref, r.get("Our_Position") or ""])
        b.append(table(rows, [5000, 1900, 2200, 1800]))
        if len(mand) > MAX_REQ_ROWS:
            b.append(para(f"{len(mand) - MAX_REQ_ROWS} more mandatory requirements are in the CRM.", italic=True))
    else:
        b.append(para("No mandatory requirements recorded yet.", italic=True, color="888888"))

    b.append(para("Decision", "Heading1"))
    dec = [["Bid decision", sol.get("Bid_Decision") or "Not decided"],
           ["Decided by", _name(sol.get("Decision_By"))],
           ["Decision date", fmt_date(sol.get("Decision_Date"))]]
    b.append(table([r for r in dec if r[1]], [3000, 7900], header=False))
    for api, label in (("Decision_Rationale", "Rationale"), ("Decision_Conditions", "Conditions")):
        b.append(para(label, "Heading2"))
        if _blank(sol.get(api)):
            b.append(para("To be completed by the team.", italic=True, color="888888"))
        else:
            b += text_blocks(sol[api])

    b.append(para("Where this came from", "Heading1"))
    if not _blank(sol.get("Header_Sources")):
        b += text_blocks(sol["Header_Sources"])
    if not _blank(sol.get("Source_Documents")):
        b.append(para("Source documents", "Heading2"))
        b += text_blocks(sol["Source_Documents"])
    b.append(para(f"Generated {now} from the Solicitation record ({ENGINE}). Rule-based, no AI. "
                  "Values are as last saved in the CRM; check the packet for anything that matters.",
                  italic=True, color="888888", size=18))
    return pack("".join(b))



def _num(x: float) -> str:
    return f"{x:g}"


def evaluation_blocks(sol: dict) -> list[str]:
    """Scoring table (points and share of total), the evaluation process, then the section as read."""
    text = sol.get("Evaluation_Criteria") or ""
    out: list[str] = []
    if sol.get("Solicitation_Type") == "Renewal Notice" and _blank(text):
        return [para("A renewal notice is not scored; no evaluation criteria were found.", italic=True, color="888888")]
    rows = evalfacts.parse_published(text) or evalfacts.points_table(text)
    out.append(para("Scoring: points and weights", "Heading2"))
    if rows:
        groups: list[str] = []
        for r in rows:
            if r.get("group", "") not in groups:
                groups.append(r.get("group", ""))
        for g in groups:
            part = [r for r in rows if r.get("group", "") == g]
            total = sum(r["n"] for r in part if r["unit"] == "points")
            if g:
                out.append(para(g.title() if g.isupper() else g, bold=True))
            tbl = [["Criterion", "Points", "Share of total"]]
            for r in part:
                share = r["share"]
                if share is None and r["unit"] == "points" and total:
                    share = r["n"] / total * 100
                if r["unit"] == "%":
                    share = r["n"]
                pts = f"{_num(r['n'])}%" if r["unit"] == "%" else _num(r["n"])
                tbl.append([r["crit"], pts, f"{share:.1f}%".replace(".0%", "%") if share is not None else ""])
            if total:
                tbl.append(["Total points read", _num(total), ""])
            out.append(table(tbl, [6300, 1900, 2700]))
        out.append(para("Points were read by rule from the evaluation section. Check them against the packet; "
                        "a criterion that is not listed here may still be scored.", italic=True, color="888888", size=18))
    else:
        out.append(para("No point values were found by rule. Read the evaluation text below.", italic=True, color="888888"))
    out.append(para("How evaluation works", "Heading2"))
    proc = sol.get("Evaluation_Process")
    if _blank(proc):
        units = [(None, t) for t in (sol.get("Evaluation_Criteria"), sol.get("Submission_Requirements"),
                                     sol.get("Scope_of_Work")) if t]
        proc = evalfacts.find_process(units, "record text")
    if _blank(proc):
        out.append(para("No evaluation process statements found.", italic=True, color="888888"))
    else:
        out += text_blocks(proc)
    if not _blank(text):
        out.append(para("Evaluation section as read from the packet", "Heading2"))
        out += [para(x, size=19, color="555555") for x in [ln for ln in (_ln.strip() for _ln in str(text).splitlines()) if ln]]
    return out


def renewal_blocks(sol: dict) -> list[str]:
    """Callout for a Solicitation_Type = Renewal Notice: what is being renewed and when we must answer."""
    block, _ = services.split_renewal_block(sol.get("Scope_of_Work"))
    out = [para("Contract renewal", "Heading1"),
           para("This is a renewal of an existing contract, not a new procurement.", bold=True, color="1F3A5F")]
    rows = []
    for api, label in (("Proposal_Due", "Renewal response due"), ("Anticipated_Start", "Renewal term starts"),
                       ("Incumbent", "Incumbent"), ("Contract_Term", "Contract term")):
        v = sol.get(api)
        if not _blank(v):
            rows.append([label, fmt_date(v) if api in ("Proposal_Due", "Anticipated_Start") else _name(v)])
    if rows:
        out.append(table(rows, [3000, 7900], header=False))
    if len(block) > 1:
        out += text_blocks("\n".join(block[1:]))
        out.append(para("Read by rule from the notice; check against the document.", italic=True, color="888888", size=18))
    else:
        out.append(para("No renewal details recorded. Press Ingest Requirements on the notice, or enter the existing "
                        "contract, renewal term and pricing in Scope of Work.", italic=True, color="B45309"))
    return out


def services_blocks(sol: dict, reqs: list[dict]) -> list[str]:
    """Services required: the Service Lines and Service Type Summary on the record, else a scan of the
    text already on the record (so a record drafted before this section existed still gets one)."""
    out = [para("Services required", "Heading1")]
    lines, summary = _name(sol.get("Service_Lines")), sol.get("Service_Type_Summary")
    drafted = False
    if _blank(lines) or _blank(summary):
        svc = services.new_services()
        for field, name in (("Scope_of_Work", "Scope of Work"), ("Submission_Requirements", "Submission Requirements"),
                            ("Key_Requirements_Summary", "Key Requirements"), ("Pricing_Model", "Pricing Model"),
                            ("Capacity_Requirement", "Capacity Requirement")):
            text = services.split_renewal_block(sol.get(field))[1] if field == "Scope_of_Work" else sol.get(field)
            found = services.find_services([(None, ln) for ln in str(text or "").splitlines()], f"{name} on the record")
            services.merge_services(svc, found)
        found = services.find_services([(None, ln) for r in reqs for ln in str(r.get("Requirement_Text") or "").splitlines()],
                                       "requirement on the record")
        services.merge_services(svc, found)
        if _blank(lines):
            lines = ", ".join(services.service_line_values(svc))
            drafted = drafted or bool(lines)
        if _blank(summary):
            summary = services.services_summary(svc)
            drafted = drafted or bool(summary)
    if not _blank(lines):
        out.append(para("Service lines: " + lines, bold=True))
    if not _blank(summary):
        out += text_blocks(summary)
    if _blank(lines) and _blank(summary):
        out.append(para("No services were found by rule. Check the scope of work in the packet and set Service Lines.",
                        italic=True, color="B45309"))
    elif drafted:
        out.append(para("Read from the text on this record; Service Lines is not set yet. Confirm before briefing.",
                        italic=True, color="888888", size=18))
    return out


def units_blocks(sol: dict, reqs: list[dict]) -> list[str]:
    """Units monitored / population counts: the drafted field, else a scan of the text already on the record."""
    out = [para("Units monitored and volumes", "Heading1")]
    txt = sol.get("Units_Monitored")
    if _blank(txt):
        lines = []
        for field, name in (("Scope_of_Work", "Scope of Work"), ("Submission_Requirements", "Submission Requirements"),
                            ("Evaluation_Criteria", "Evaluation Criteria"), ("Key_Requirements_Summary", "Key Requirements"),
                            ("Pricing_Model", "Pricing Model"), ("Capacity_Requirement", "Capacity Requirement")):
            for ln in str(sol.get(field) or "").splitlines():
                lines.append((None, ln, name))
        for r in reqs:
            for ln in str(r.get("Requirement_Text") or "").splitlines():
                lines.append((None, ln, "requirement"))
        found = []
        for name in sorted({x[2] for x in lines}):
            found += evalfacts.find_volumes([(pg, ln) for pg, ln, nm in lines if nm == name], f"{name} on the record")
        seen, uniq = set(), []
        for v in found:
            if (v["n"], v["noun"]) not in seen:
                seen.add((v["n"], v["noun"]))
                uniq.append(v)
        txt = evalfacts.volume_lines(uniq[:12])
    for k, label in (("Capacity_Requirement", "Capacity requirement"), ("Working_Volume_Estimate", "Our working estimate")):
        if not _blank(sol.get(k)):
            out.append(para(f"{label}: {sol[k]}", bold=True))
    if _blank(txt):
        out.append(para("No offender, participant or device counts were found. Check the packet for an average "
                        "daily population or device quantities.", italic=True, color="B45309"))
    else:
        out += text_blocks(txt)
    return out


# ---------- run ----------
def run(crm: Crm, sol_id: str, dry_run: bool = False) -> str:
    sol = crm.get_record("Solicitations", sol_id)
    sol_lk = crm.lookup_field_to("Solicitation_Requirements", "Solicitations")
    try:
        reqs = crm.coql("select Name, Requirement_Text, Mandatory, Category, Source_Reference, Page_PDF, Our_Position "
                        f"from Solicitation_Requirements where {sol_lk} = {int(sol_id)}")
    except CrmError:
        reqs = crm.coql("select Name, Requirement_Text, Mandatory, Category "
                        f"from Solicitation_Requirements where {sol_lk} = {int(sol_id)}")
    data = build(sol, reqs)
    safe = "".join(c for c in (sol.get("Name") or "solicitation") if c.isalnum() or c in " -_()").strip()[:60]
    fname = f"{BRIEFING_PREFIX}{safe} - {time.strftime('%Y-%m-%d %H%M', time.gmtime())}.docx"
    if dry_run:
        return f"dry run: would attach {fname} ({len(data)} bytes)"
    crm.upload_attachment("Solicitations", sol_id, fname, data)
    return (f"Briefing built and attached to this Solicitation as '{fname}'. Open the Attachments "
            "related list (refresh the page). Rebuild any time; earlier copies are kept.")

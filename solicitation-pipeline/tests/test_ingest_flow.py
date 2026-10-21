"""End-to-end rule ingest against an in-memory CRM: text PDF, scanned PDF, Form K-style
XLSX, unsupported file; dedupe, lock, re-read, time-budget stop and resume. No AI, no network.

Fixtures are built on the fly (reportlab + openpyxl if installed; the XLSX part falls back
to a hand-written minimal workbook)."""
from __future__ import annotations

import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solpipe import extract, ingest, rule_ingest  # noqa: E402

CLOCK = [1_000_000.0]
ingest.time = type("T", (), {"time": staticmethod(lambda: CLOCK[0])})

RFP_LINES = [
    ["3.4 Minimum Qualifications",
     "3.4.1 The Contractor shall maintain commercial general liability insurance of at least $1,000,000.",
     "3.4.2 Offerors must provide three references from similar contracts within the last five years.",
     "The City welcomes proposals from all vendors."],
    ["4.3 Battery and Continuity",
     "4.3.1 Each GPS device shall have a battery life of at least 40 hours and tamper alerts.",
     "Vendors should describe their training approach for agency staff."],
]


def text_pdf() -> bytes:
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for page in RFP_LINES:
        y = 800
        for line in page:
            c.drawString(40, y, line)
            y -= 20
        c.showPage()
    c.save()
    return buf.getvalue()


def scanned_pdf() -> bytes:
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.rect(40, 400, 400, 300, fill=1)  # a picture, no text layer
    c.showPage()
    c.save()
    return buf.getvalue()


def form_k_xlsx() -> bytes:
    sheet = [["City of Charlotte - Form K Requirements Matrix"], [],
             ["Item", "Specification", "Mandatory (M) / Desired (D)", "Comply Y/N"],
             ["General"],
             ["K-1", "Device shall be waterproof to 15 feet", "M", ""],
             ["K-2", "Device weight under 6 oz", "D", ""],
             ["Battery"],
             ["K-3", "Battery life of at least 40 hours", "M", ""]]
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Form K"
        for r in sheet:
            ws.append(r)
        b = io.BytesIO()
        wb.save(b)
        return b.getvalue()
    except ImportError:
        pass
    rows = []
    for i, r in enumerate(sheet, 1):
        cells = "".join(f'<c r="{chr(65 + j)}{i}" t="inlineStr"><is><t>{v}</t></is></c>' for j, v in enumerate(r) if v)
        rows.append(f'<row r="{i}">{cells}</row>')
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   '<sheet name="Form K" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   f'<sheetData>{"".join(rows)}</sheetData></worksheet>')
    return b.getvalue()


class FakeCrm:
    FIELDS = {"Name", "Solicitation", "Requirement_Text", "Section_Group", "Priority", "Category", "Source_Reference",
              "Mandatory", "Our_Position", "Page_PDF"}

    def __init__(self, files):
        self.sol = {"id": "1", "Name": "T", "Ingest_Queue": None, "Ingested_Attachments": None, "Extraction_Notes": None}
        self.files = files
        self.reqs = []
        self.downloads = 0

    def get_record(self, m, rid):
        return dict(self.sol)

    def lookup_field_to(self, m, t):
        return "Solicitation"

    def fields(self, m):
        return [{"api_name": f} for f in self.FIELDS]

    def attachments(self, m, rid):
        return [{"id": k, "File_Name": v[0]} for k, v in self.files.items()]

    def download_attachment(self, m, rid, aid):
        self.downloads += 1
        CLOCK[0] += 50  # each document costs 50 simulated seconds
        return self.files[aid][1]

    def coql(self, q):
        return [{"Name": r["Name"], "Requirement_Text": r["Requirement_Text"]} for r in self.reqs]

    def create_many(self, m, rows, trigger=()):
        for r in rows:
            assert set(r) <= self.FIELDS, set(r) - self.FIELDS
            for k, lim in ingest.TEXT_LIMITS.items():
                assert r.get(k) is None or len(r[k]) <= lim, (k, r[k])
        self.reqs.extend(rows)
        return [str(i) for i in range(len(rows))]

    def update(self, m, rid, f, trigger=()):
        self.sol.update(f)

    def field(self, m, f):
        return {"data_type": "text"} if f in ("Parse_Date", "Key_Dates_Verified", "Parsed_By") else None


try:
    import reportlab  # noqa: F401
    import pdfminer  # noqa: F401
    HAVE_PDF = True
except ImportError:
    HAVE_PDF = False


def test_pdf_parse():
    if not HAVE_PDF:
        print("skip: needs reportlab + pdfminer.six to build and read PDF fixtures")
        return
    pages = rule_ingest.extract_pdf_pages(text_pdf())
    assert len(pages) == 2
    res = rule_ingest.parse_pages(pages)
    got = [(r["Source_Reference"], r["Page_PDF"], r["Mandatory"], r["Category"]) for r in res["rows"]]
    assert got == [("3.4.1", "1", True, "Insurance"), ("3.4.2", "1", True, "Experience / Refs"),
                   ("4.3.1", "2", True, "Technology / Device"), (None, "2", False, "Staffing")], got
    assert res["rows"][2]["Section_Group"] == "4.3 Battery and Continuity"
    assert rule_ingest.parse_pages(rule_ingest.extract_pdf_pages(scanned_pdf()))["needs_manual"]


def test_form_k_table():
    doc = extract.to_document("formk.xlsx", form_k_xlsx())
    res = extract.parse_document(doc)
    got = [(r["Source_Reference"], r["Mandatory"], r["Section_Group"]) for r in res["rows"]]
    assert got == [("K-1", True, "General"), ("K-2", False, "General"), ("K-3", True, "Battery")], got
    assert "table, 3 rows" in res["notes"]


def test_full_run_and_resume():
    if not HAVE_PDF:
        print("skip: needs reportlab + pdfminer.six to build and read PDF fixtures")
        return
    files = {"a1": ("rfp.pdf", text_pdf()), "a2": ("formk.xlsx", form_k_xlsx()),
             "a3": ("scan.pdf", scanned_pdf()), "a4": ("logo.png", b"\x89PNG")}
    crm = FakeCrm(files)
    # budget: 60 s margin + 50 s per document -> room for two documents on the first press
    msg = ingest.run(crm, "1", budget_s=150)
    assert "1 document(s) still to go" in msg, msg
    assert set(crm.sol["Ingested_Attachments"].split("\n")) == {"a1", "a2"}
    msg2 = ingest.run(crm, "1", budget_s=600)
    assert "All attached documents are read" in msg2, msg2
    assert "scan.pdf" in msg2 and "enter requirements by hand" in msg2 and "logo.png" in msg2
    q = {l[0]: l[2] for l in ingest.parse_queue(crm.sol["Ingest_Queue"])}
    assert q == {"a1": "DONE", "a2": "DONE", "a3": "MANUAL"}, q
    assert len(crm.reqs) == 7 and all(r["Our_Position"] == "Unverified" for r in crm.reqs)
    assert crm.reqs[0]["Source_Reference"] == "rfp.pdf, §3.4.1, p.1"
    assert "no AI" in crm.sol["Extraction_Notes"] and "rule-ingest" in crm.sol["Parsed_By"]
    assert not crm.sol["Ingest_Queue"].startswith("#RUNNING"), "lock must be released"
    # a fresh lock blocks a concurrent press; a stale one does not
    crm.sol["Ingest_Queue"] = f"#RUNNING {CLOCK[0]:.0f}\n" + crm.sol["Ingest_Queue"]
    assert "still working" in ingest.run(crm, "1", budget_s=600)
    CLOCK[0] += 21 * 60
    # clearing an attachment id re-reads that document and dedupes to zero new rows
    crm.sol["Ingested_Attachments"] = "a2\na3"
    n = len(crm.reqs)
    msg3 = ingest.run(crm, "1", budget_s=600)
    assert len(crm.reqs) == n and "0 draft requirement(s) created, 4 duplicate(s)" in msg3, msg3
    # and a press with nothing new is a no-op
    before = crm.downloads
    assert "already read earlier" in ingest.run(crm, "1", budget_s=600) and crm.downloads == before


def test_zip_attachment():
    if not HAVE_PDF:
        print("skip: needs reportlab + pdfminer.six to build and read PDF fixtures")
        return
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Charlotte RFP/rfp.pdf", text_pdf())
        z.writestr("Charlotte RFP/Form K.xlsx", form_k_xlsx())
        z.writestr("Charlotte RFP/scan.pdf", scanned_pdf())
        z.writestr("Charlotte RFP/logo.png", b"\x89PNG")
        z.writestr("Charlotte RFP/inner.zip", b"PK")
        z.writestr("__MACOSX/Charlotte RFP/._rfp.pdf", b"junk")
        z.writestr("Charlotte RFP/", b"")
    crm = FakeCrm({"z1": ("packet.zip", b.getvalue()), "z2": ("broken.zip", b"not a zip")})
    msg = ingest.run(crm, "1", budget_s=600)
    assert len(crm.reqs) == 7, len(crm.reqs)
    assert crm.reqs[0]["Source_Reference"].startswith("packet.zip > rfp.pdf, §3.4.1, p.1"), crm.reqs[0]["Source_Reference"]
    assert "packet.zip > scan.pdf" in msg and "enter requirements by hand" in msg
    assert "logo.png (unsupported file type)" in msg and "zip inside a zip" in msg and "__MACOSX" not in msg
    q = {l[0]: (l[1], l[2], l[3]) for l in ingest.parse_queue(crm.sol["Ingest_Queue"])}
    assert q["z1"][:2] == ("3 files", "DONE") and "7 new" in q["z1"][2] and "by hand: scan.pdf" in q["z1"][2], q["z1"]
    assert q["z2"][1] == "FAILED" and "not a valid .zip" in q["z2"][2], q["z2"]
    # a second press re-reads nothing
    n = len(crm.reqs)
    assert "already read earlier" in ingest.run(crm, "1", budget_s=600) and len(crm.reqs) == n


def test_briefing_drafted_in_run():
    txt = ("3. SCOPE OF WORK\nThe Contractor shall provide GPS monitoring for up to 120 offenders across the county.\n"
           "Services are required throughout Colin County, Texas.\n"
           "4. EVALUATION CRITERIA\nTechnical approach - 60 points\nPrice - 40 points\n"
           "5. SCHEDULE\nProposals are due: October 20, 2026 at 2:00 PM Central\n"
           "6. FUNDING\nThe estimated annual budget is $900,000.\n"
           "The Contractor shall maintain commercial general liability insurance of at least $1,000,000.\n")
    crm = FakeCrm({"t1": ("rfp.txt", txt.encode())})
    crm.sol["Scope_of_Work"] = "Written by hand"
    msg = ingest.run(crm, "1", budget_s=600)
    assert crm.sol["Scope_of_Work"] == "Written by hand", "an existing value must never be overwritten"
    assert crm.sol["Evaluation_Criteria"].startswith("Published points: Technical approach - 60 points; Price - 40 points")
    assert crm.sol["Proposal_Due"] == "2026-10-20T14:00:00-05:00", crm.sol.get("Proposal_Due")
    assert crm.sol["Stated_Budget"] == 900000.0 and crm.sol["Budget_Disclosed"] is True
    assert crm.sol["Briefing_Status"] == "Drafted from packet"
    assert "Briefing drafted" in msg and "Scope Of Work" not in msg, msg
    # a second press with nothing new does not rewrite or duplicate anything
    assert "already read earlier" in ingest.run(crm, "1", budget_s=600)


def test_no_attachments_message():
    crm = FakeCrm({})
    assert "No attachments found" in ingest.run(crm, "1", budget_s=600)


if __name__ == "__main__":
    for k, fn in list(globals().items()):
        if k.startswith("test_") and callable(fn):
            fn()
            print("ok  ", k)
    print("ok")

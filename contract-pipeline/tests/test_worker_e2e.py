# End-to-end run of con_worker.main._ingest_contract against a fake Zoho client, with a real zip of real
# PDFs (fixture text rendered to PDF with reportlab, so pdfplumber's line-by-line output is exercised).
import io
import os
import sys
import zipfile

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "con_worker"))
reportlab = pytest.importorskip("reportlab")
pytest.importorskip("pdfplumber")

import types  # noqa: E402

sys.modules.setdefault("zoho_client", types.ModuleType("zoho_client"))  # real client needs Catalyst env vars

from reportlab.lib.pagesizes import letter  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402


def _pdf(text):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for page in text.split("\n"):
        y = 750
        words, line = page.split(), ""
        for w in words:
            if len(line) + len(w) > 95:
                c.drawString(40, y, line)
                y -= 12
                line = ""
                if y < 40:
                    c.showPage()
                    y = 750
            line += w + " "
        c.drawString(40, y, line)
        c.showPage()
    c.save()
    return buf.getvalue()


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, fixture in files:
            with open(os.path.join(HERE, "fixtures", fixture + ".txt"), encoding="utf-8") as f:
                z.writestr(name, _pdf(f.read()))
    return buf.getvalue()


class FakeZoho:
    def __init__(self, contract, zip_bytes):
        self.contract = contract
        self.zip = zip_bytes
        self.updates, self.notes, self.docs, self.clauses = [], [], [], []
        self._next = 900

    def get_record(self, module, rid, fields=None):
        return self.contract

    def list_attachments(self, module, rid):
        return [{"id": "a1", "File_Name": "packet.zip"}]

    def download_attachment(self, module, rid, aid):
        return self.zip

    def coql(self, q):
        if "from Contract_Clauses" in q:
            return [{"id": "k", "Clause_Hash": c["Clause_Hash"], "Status": c["Status"]} for c in self.clauses]
        return []

    def create_record(self, module, data):
        if module == "Contract_Clauses":
            self.clauses.append(data)
            return {"details": {"id": "k{}".format(len(self.clauses))}}
        self.docs.append(data)
        return {"details": {"id": "d{}".format(len(self.docs))}}

    def update_record(self, module, rid, data):
        self.updates.append(data)
        if "Rate_Lines" in data:  # emulate subform replace semantics: rows not sent are deleted
            keep = []
            by_id = {r["id"]: r for r in self.contract["Rate_Lines"]}
            for item in data["Rate_Lines"]:
                if "id" in item:
                    r = dict(by_id[item["id"]])
                    r.update(item)
                else:
                    self._next += 1
                    r = dict(item, id=str(self._next))
                keep.append(r)
            self.contract["Rate_Lines"] = keep
        else:
            self.contract.update(data)

    def post_note(self, module, rid, title, content):
        self.notes.append(content)


def test_allied_standard_end_to_end(monkeypatch):
    import main
    from test_golden_3b_3c import EXISTING_ALLIED_STD
    contract = {"id": "c1", "Counterparty_Type": "Vendor", "Account": None, "Ingest_Status": None,
                "Rate_Lines": [dict(r) for r in EXISTING_ALLIED_STD]}
    fake = FakeZoho(contract, _zip([
        ("Addendum 13 - Tiered Pricing and Spare Fee 2022.pdf", "allied_add13"),
        ("Addendum 23 w Exhibit 2-Justice Point for Sheboygan County Sheriff.pdf", "allied_add23"),
        ("Addendum 24 - New Rate 401 plus and Upper Spares.pdf", "allied_add24"),
    ]))
    monkeypatch.setattr(main, "zc", fake)
    main._ingest_contract("c1")

    rows = contract["Rate_Lines"]
    assert len(rows) == 8  # 7 existing kept (none deleted) + the superseded 2.92 row
    r292 = next(r for r in rows if r["Rate_Daily_Amount"] == 2.92)
    r282 = next(r for r in rows if r["Rate_Daily_Amount"] == 2.82)
    assert r292["Superseded_By"] == r282["id"] and r292["Effective_Date"] == "2022-06-01"
    assert "p.1" in r282["Source_Ref"] and r282["Effective_Date"] == "2026-02-01"
    assert contract["Ingest_Status"] == "Needs Review"
    assert contract["Spare_Clause_Detail"].startswith("Spares above 35%")
    add24 = next(d for d in fake.docs if d["File_Name"].startswith("Addendum 24"))
    assert add24["Applies_To_Contracts"] == "592,835,918,943,966,993,1023"
    assert add24["Effective_Date"] == "2026-02-01" and add24["Doc_Type"] == "Addendum"
    assert "CONFLICT" not in fake.notes[0]
    # Phase 3d: the golden Sheboygan exclusion is written as a Proposed Contract_Clauses row
    ex = [c for c in fake.clauses if "not eligible" in c["Clause_Text"]]
    assert len(ex) == 1 and ex[0]["Status"] == "Proposed" and ex[0]["Clause_Type"] == "Special work"
    assert ex[0]["Name"] and ex[0]["Contract"] == {"id": "c1"} and "Addendum 23" in ex[0]["Source_Ref"]
    assert "CLAUSES:" in fake.notes[0] and "created as Contract_Clauses rows" in fake.notes[0]
    n_clauses = len(fake.clauses)

    # rerun with the same bytes: Packet_Hash short-circuit
    main._ingest_contract("c1")
    assert "already read earlier" in fake.notes[-1]
    assert len(fake.clauses) == n_clauses

    # a changed packet re-runs the finder: existing hashes are skipped, nothing is duplicated
    contract["Packet_Hash"] = "changed"
    main._ingest_contract("c1")
    assert len(fake.clauses) == n_clauses


def test_scram_end_to_end(monkeypatch):
    import main
    from test_golden_3b_3c import EXISTING_SCRAM
    contract = {"id": "c2", "Counterparty_Type": "Vendor", "Account": None, "Ingest_Status": None,
                "Rate_Lines": [dict(r) for r in EXISTING_SCRAM]}
    fake = FakeZoho(contract, _zip([
        ("Justice Point_Master SP Agreement_Expires 20270109.pdf", "ams_master"),
        ("Justice Point_Product and Service Schedule_Expires 20270109.pdf", "ams_schedule"),
    ]))
    monkeypatch.setattr(main, "zc", fake)
    main._ingest_contract("c2")
    assert contract["Contract_Start_Date"] == "2024-01-10" and contract["Contract_End_Date"] == "2027-01-09"
    assert contract["Auto_Renews"] == "Yes" and contract["Notice_Period_Days"] == 30
    rows = {r["Rate_Device"]: r for r in contract["Rate_Lines"]}
    assert rows["CAM Landline"]["Source_Ref"] and rows["CAM"]["Rate_Daily_Amount"] == 6.94
    assert "CONFLICT" in fake.notes[0]
    # Phase 3d on real PDF text (pdfplumber line output, not the fixture's flat text)
    assert 8 <= len(fake.clauses) <= 15, [c["Title"] for c in fake.clauses]
    assert not any("indemnif" in c["Clause_Text"].lower() for c in fake.clauses)
    debrand = [c for c in fake.clauses if "remove all reference to AMS" in c["Clause_Text"]]
    assert debrand and debrand[0]["Due_Date"] == "2027-02-08"   # 30 days after Contract_End_Date


# Catalyst job entry point: params via get_job_param, job closed explicitly, failures reach the record
class _JobReq:
    def __init__(self, **p):
        self.p = p

    def get_job_param(self, k):
        return self.p.get(k)


class _Ctx:
    def __init__(self):
        self.closed = None

    def close_with_success(self):
        self.closed = "ok"

    def close_with_failure(self):
        self.closed = "fail"


def test_job_handler_success_and_failure(monkeypatch):
    import main
    seen = []
    monkeypatch.setattr(main, "_ingest_contract", lambda cid: seen.append(cid))
    ctx = _Ctx()
    main.handler(_JobReq(module="Contracts", recordId="c9", action="ingest_contract"), ctx)
    assert seen == ["c9"] and ctx.closed == "ok"

    fake = FakeZoho({"id": "c9", "Rate_Lines": []}, b"")
    monkeypatch.setattr(main, "zc", fake)

    def boom(cid):
        raise RuntimeError("zip is corrupt")
    monkeypatch.setattr(main, "_ingest_contract", boom)
    ctx = _Ctx()
    main.handler(_JobReq(module="Contracts", recordId="c9", action="ingest_contract"), ctx)
    assert ctx.closed == "fail" and "zip is corrupt" in fake.notes[-1]
    assert fake.contract["Ingest_Status"] == "Failed"

import io, os, sys, unittest, zipfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from solpipe import briefdoc, extract  # noqa: E402

SOL = {"Name": "Colin County (TX) EM RFP", "Solicitation_Number": "2026-14", "Status": "Briefing",
       "Issuing_Agency_Text": "Colin County", "Briefing_Status": "Drafted from packet",
       "Proposal_Due": "2026-10-20T14:00:00-04:00", "Release_Date": "2026-09-01",
       "Key_Dates_Verified": False, "Pre_Proposal_Conference": "2026-09-22T10:00:00-05:00", "Pre_Proposal_Mandatory": True,
       "Scope_of_Work": "GPS monitoring for 120 offenders.\n- 24/7 alerts\n- <Monitoring & reports>",
       "Stated_Budget": 450000, "Budget_Disclosed": True,
       "Evaluation_Criteria": "Published points: Technical 40; Price 30",
       "Header_Sources": "Proposal_Due: 'due October 20' (RFP.pdf p.3)"}
REQS = [{"Name": "R1", "Requirement_Text": "Carry $1M insurance", "Mandatory": True, "Category": "Insurance",
         "Source_Reference": "RFP.pdf", "Page_PDF": "12", "Our_Position": "Unverified"},
        {"Name": "R2", "Requirement_Text": "optional thing", "Mandatory": False}]


class T(unittest.TestCase):
    def test_docx_is_valid_and_readable(self):
        data = briefdoc.build(SOL, REQS, now="test")
        z = zipfile.ZipFile(io.BytesIO(data))
        self.assertIsNone(z.testzip())
        txt = extract.docx_text(data)
        for needle in ("Colin County (TX) EM RFP", "Tue Oct 20, 2026 1:00 PM Central", "MANDATORY", "$450,000",
                       "Carry $1M insurance", "DRAFT: dates", "<Monitoring & reports>", "To be completed by the team"):
            self.assertIn(needle, txt)
        self.assertNotIn("optional thing", txt)

    def test_empty_solicitation_does_not_crash(self):
        txt = extract.docx_text(briefdoc.build({"Name": "x"}, []))
        self.assertIn("Nothing recorded yet", txt)

    def test_ingest_skips_briefing_attachments(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "solpipe", "ingest.py"),
                   encoding="utf-8").read()
        self.assertIn("BRIEFING_PREFIX", src)

    def test_no_ai(self):
        src = open(briefdoc.__file__, encoding="utf-8").read().lower()
        for w in ("anthropic", "openai", "claude"):
            self.assertNotIn(w, src.replace("rule-based, no ai", ""))


class T2(unittest.TestCase):
    def test_points_process_and_units_in_doc(self):
        sol = dict(SOL)
        sol["Evaluation_Criteria"] = ("Evaluation Criteria (a.zip > r.pdf, p.14)\n• Scoring Method\n• Weight (Points) 1.\n"
            "• Device Meeting Requirements\n• Points Based\n• See Attachment A\n• demonstration of the unit 14 40 (50% of Total)\n"
            "• Request For Proposal #2026-347\n• Title: GPS Offender Monitoring 2.\n• Services Provided, Project Team,\n"
            "• Project Plan\n• Points Based 30 (37.5% of Total)\nProposals will be evaluated by a committee. Finalists give a demonstration.")
        sol["Scope_of_Work"] = "The County currently monitors an average daily population of 250 offenders with GPS devices."
        txt = extract.docx_text(briefdoc.build(sol, []))
        self.assertIn("Device Meeting Requirements | 40 | 50%", txt)
        self.assertIn("Services Provided, Project Team, Project Plan | 30 | 37.5%", txt)
        self.assertIn("Total points read | 70", txt)
        self.assertIn("evaluated by a committee", txt)
        self.assertIn("250 offenders", txt)

    def test_units_field_wins(self):
        sol = dict(SOL, Units_Monitored="• 400 active units (maximum / capacity): \"up to 400\" (r.pdf, p.3)")
        self.assertIn("400 active units", extract.docx_text(briefdoc.build(sol, [])))


if __name__ == "__main__":
    unittest.main()

import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solpipe import briefing  # noqa: E402

TOC = "TABLE OF CONTENTS\n3. Scope of Work ........ 7\n4. Evaluation Criteria ........ 12\n"
PAGE = """3. SCOPE OF WORK
3.1 Services Requested
The Contractor shall provide GPS electronic monitoring for up to 120 offenders
and 40 radio frequency units across the county.
3.2 Service Area
Services are required throughout Colin County, Texas.
4. EVALUATION CRITERIA
Proposals will be scored as follows:
Technical approach - 40 points
Price - 30 points
Experience and references - 30 points
5. SUBMISSION REQUIREMENTS
Proposals must be submitted electronically through the county portal as one PDF.
Do not exceed 40 pages excluding forms.
6. SCHEDULE
Issue date: September 1, 2026
Deadline for written questions: September 15, 2026 at 3:00 PM CT
Proposals are due: October 20, 2026 at 2:00 PM Eastern
Anticipated award date: December 1, 2026
7. FUNDING
The estimated annual budget for this contract is $1,250,000.
"""


def doc(pages):
    return {"kind": "pdf", "pages": pages}


class Briefing(unittest.TestCase):
    def setUp(self):
        self.acc = briefing.merge(briefing.new_findings(), briefing.scan("rfp.pdf", doc([TOC, PAGE])))

    def test_sections_skip_toc_and_cite_page(self):
        s = self.acc["sections"]
        self.assertIn("Scope_of_Work", s)
        text = s["Scope_of_Work"][0]
        self.assertIn("rfp.pdf, p.2", text)
        self.assertIn("120 offenders", text)
        self.assertIn("Colin County", text)           # sub-sections stay inside the scope section
        self.assertNotIn("Technical approach", text)  # next top-level section ends it
        self.assertIn("one PDF", s["Submission_Requirements"][0])

    def test_points_and_weights_flag(self):
        upd, _ = briefing.fields_for({}, self.acc, [])
        self.assertTrue(upd["Evaluation_Criteria"].startswith("Published points: Technical approach - 40 points"))
        self.assertTrue(upd["Eval_Weights_Published"])

    def test_dates(self):
        upd, _ = briefing.fields_for({}, self.acc, [])
        self.assertEqual(upd["Release_Date"], "2026-09-01")
        self.assertEqual(upd["Anticipated_Award"], "2026-12-01")
        self.assertEqual(upd["Questions_Round_1_Due"], "2026-09-15T15:00:00-05:00")   # CT in September is CDT
        self.assertEqual(upd["Proposal_Due"], "2026-10-20T14:00:00-04:00")            # Eastern, EDT
        self.assertIn("Packet says Eastern", upd["Deadline_Timezone_Note"])
        self.assertIn("Questions_Round_1_Due", upd["Header_Sources"])

    def test_budget_single_amount(self):
        upd, _ = briefing.fields_for({}, self.acc, [])
        self.assertEqual(upd["Stated_Budget"], 1250000.0)
        self.assertTrue(upd["Budget_Disclosed"])

    def test_budget_ambiguous_left_blank(self):
        acc = briefing.merge(briefing.new_findings(), briefing.scan("a.txt", {"kind": "text", "text":
            "The budget is $500,000 for year one.\nThe not to exceed amount is $2.5 million.\n"}))
        upd, notes = briefing.fields_for({}, acc, [])
        self.assertNotIn("Stated_Budget", upd)
        self.assertTrue(any("Several dollar amounts" in n for n in notes))

    def test_never_overwrites(self):
        sol = {"Scope_of_Work": "Beau wrote this", "Proposal_Due": "2026-10-21T10:00:00-05:00", "Stated_Budget": 9.0,
               "Evaluation_Criteria": "kept"}
        upd, _ = briefing.fields_for(sol, self.acc, [])
        for k in ("Scope_of_Work", "Proposal_Due", "Stated_Budget", "Evaluation_Criteria"):
            self.assertNotIn(k, upd)
        self.assertIn("Submission_Requirements", upd)

    def test_time_and_zone_placeholders_flagged(self):
        acc = briefing.merge(briefing.new_findings(), briefing.scan("a.txt", {"kind": "text", "text":
            "Proposals are due: November 3, 2026\n"}))
        upd, _ = briefing.fields_for({}, acc, [])
        self.assertEqual(upd["Proposal_Due"], "2026-11-03T12:00:00-06:00")
        self.assertIn("12:00 PM placeholder", upd["Header_Sources"])
        self.assertIn("read as Central", upd["Header_Sources"])

    def test_status_set_only_when_unstarted(self):
        self.assertEqual(briefing.fields_for({}, self.acc, [])[0]["Briefing_Status"], "Drafted from packet")
        self.assertNotIn("Briefing_Status", briefing.fields_for({"Briefing_Status": "Reviewed by RFP team"}, self.acc, [])[0])

    def test_key_requirements_summary(self):
        rows = [{"Requirement_Text": "Maintain liability insurance of $1,000,000.", "Mandatory": True, "Category": "Insurance",
                 "Source_Reference": "3.4.1", "Page_PDF": "12"},
                {"Requirement_Text": "Describe training.", "Mandatory": False, "Category": None}]
        s = briefing.key_requirements_summary(rows)
        self.assertTrue(s.startswith("1 mandatory of 2 requirements read."))
        self.assertIn("[Insurance] Maintain liability insurance", s)
        self.assertIn("3.4.1, p.12", s)
        self.assertEqual(briefing.key_requirements_summary([]), "")

    def test_empty_doc(self):
        acc = briefing.merge(briefing.new_findings(), briefing.scan("x.txt", {"kind": "text", "text": "Nothing useful here at all."}))
        self.assertEqual(briefing.fields_for({}, acc, [])[0], {})

    def test_no_ai(self):
        src = (pathlib.Path(__file__).parent.parent / "solpipe" / "briefing.py").read_text().lower()
        for bad in ("anthropic", "openai", "urllib", "requests", "import socket"):
            self.assertNotIn(bad, src)


class TestEvalFacts(unittest.TestCase):
    def test_ingest_drafts_process_and_units(self):
        doc = {"kind": "text", "text": ("3. SCOPE OF WORK\nThe County currently monitors an average daily population of 250 offenders "
               "with GPS devices across pre-trial release.\n4. EVALUATION CRITERIA\nAll proposals will be evaluated by a committee of County staff using the criteria below. "
               "A minimum score of 70 is required to advance.\nTechnical - 40 points\nPrice - 30 points\n")}
        acc = briefing.merge(briefing.new_findings(), briefing.scan("rfp.txt", doc))
        upd, _ = briefing.fields_for({}, acc, [])
        self.assertIn("250 offenders", upd["Units_Monitored"])
        self.assertIn("evaluated by a committee", upd["Evaluation_Process"])
        upd2, _ = briefing.fields_for({"Units_Monitored": "mine", "Evaluation_Process": "mine"}, acc, [])
        self.assertNotIn("Units_Monitored", upd2)
        self.assertNotIn("Evaluation_Process", upd2)


if __name__ == "__main__":
    unittest.main()

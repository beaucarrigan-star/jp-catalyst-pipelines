"""Services required and contract renewals: the ingest reads them, the briefing shows them. No AI, no network."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solpipe import briefdoc, briefing, extract, ingest, services  # noqa: E402
from tests.test_ingest_flow import FakeCrm  # noqa: E402

RFP = """TABLE OF CONTENTS
3. Scope of Work ........ 7
3. SCOPE OF WORK
3.1 Services Requested
The Contractor shall provide GPS electronic monitoring for up to 120 offenders
and 40 radio frequency units across the county.
The Contractor shall provide continuous alcohol monitoring (CAM) for DWI court participants.
The vendor will also operate a 24/7 call center for officer support.
The Contractor shall provide pretrial services and a diversion program for eligible defendants.
The County will provide a project implementation resource for the Pre-Trial Release department.
All offerors will receive equal treatment under this procurement.
4. EVALUATION CRITERIA
Technical - 60 points
Price - 40 points
"""

NOTICE = """Notice of Contract Renewal
Contract No. 2023-114
Dear Vendor:
The County intends to exercise its option to renew the electronic monitoring agreement for two (2) one-year renewal terms.
The current term of the agreement expires on December 31, 2026.
Pricing for the renewal term will remain unchanged except for a 3% CPI adjustment.
Please return the signed renewal acceptance no later than November 2, 2026 at 5:00 PM Central.
The renewal term begins on January 1, 2027.
"""


def text(t):
    return {"kind": "text", "text": t}


def pdf_pages(pages):
    return {"kind": "pdf", "pages": pages}


class Services(unittest.TestCase):
    def setUp(self):
        self.acc = briefing.merge(briefing.new_findings(), briefing.scan("rfp.pdf", pdf_pages([RFP])))

    def test_service_lines_and_evidence(self):
        svc = self.acc["services"]
        self.assertEqual(services.service_line_values(svc),
                         ["EM GPS", "EM RF / RB", "EM Alcohol / CAM", "Pretrial", "Diversion"])
        self.assertIn("GPS electronic monitoring", svc["lines"]["EM GPS"]["text"])
        self.assertEqual(svc["lines"]["EM GPS"]["page"], "1")

    def test_other_is_never_picked_by_rule(self):
        svc = briefing.merge(briefing.new_findings(), briefing.scan("a.txt", text(
            "The Contractor shall provide GPS monitoring for 50 offenders.\n"
            "The Contractor shall provide ongoing training and monthly reports."))).get("services")
        self.assertEqual(services.service_line_values(svc), ["EM GPS"])
        self.assertEqual(len(svc["duties"]), 2)         # the training and report duties are still listed

    def test_departments_and_county_duties_are_not_services(self):
        svc = briefing.merge(briefing.new_findings(), briefing.scan("a.txt", text(
            "This RFP is for the Pre-Trial Release, Community Supervision and Corrections Department.\n"
            "The County will provide a project implementation resource.\n"
            "The Evaluation Committee will conduct a detailed assessment of all proposals.\n"))).get("services")
        self.assertEqual(svc["lines"], {})
        self.assertEqual(svc["duties"], [])

    def test_spreadsheet_rows_are_separate_statements(self):
        grid = [(1, ["General Requirements"]), (2, ["1", "Contractor shall be responsible for installing the GPS monitors"]),
                (3, ["2", "The County shall not be responsible for lost equipment"])]
        doc = {"kind": "xlsx", "sheets": [("Form K", grid)]}
        duties = [d["text"] for d in briefing.scan("k.xlsx", doc)["services"]["duties"]]
        self.assertEqual(duties, ["Contractor shall be responsible for installing the GPS monitors"])

    def test_generic_words_do_not_trigger_treatment(self):
        self.assertNotIn("Treatment", self.acc["services"]["lines"])     # "equal treatment"

    def test_duties_cite_page_and_skip_contents(self):
        duties = [d["text"] for d in self.acc["services"]["duties"]]
        self.assertTrue(any(d.startswith("The Contractor shall provide GPS electronic monitoring for up to 120 offenders and 40 radio")
                            for d in duties), duties)          # the heading lines above it are not glued on
        self.assertTrue(any("24/7 call center" in d for d in duties))
        self.assertFalse(any("........" in d for d in duties))

    def test_fields_for_drafts_empty_fields_only(self):
        upd, _ = briefing.fields_for({}, self.acc, [])
        self.assertIn("EM GPS", upd["Service_Lines"])
        self.assertIn("Services required (read by rule", upd["Service_Type_Summary"])
        self.assertIn("(rfp.pdf, p.1)", upd["Service_Type_Summary"])
        self.assertNotIn("Solicitation_Type", upd)                        # an RFP stays an RFP
        upd2, _ = briefing.fields_for({"Service_Lines": ["Pretrial"], "Service_Type_Summary": "mine"}, self.acc, [])
        self.assertNotIn("Service_Lines", upd2)
        self.assertNotIn("Service_Type_Summary", upd2)

    def test_nothing_found_drafts_nothing(self):
        acc = briefing.merge(briefing.new_findings(), briefing.scan("x.txt", text("Nothing useful here at all.")))
        upd, _ = briefing.fields_for({}, acc, [])
        self.assertEqual(upd, {})


class Renewals(unittest.TestCase):
    def setUp(self):
        self.acc = briefing.merge(briefing.new_findings(), briefing.scan("renewal.docx", text(NOTICE)))

    def test_notice_detected_and_facts_read(self):
        ren = self.acc["renewal"]
        self.assertTrue(ren["notice"])
        f = ren["facts"]
        self.assertEqual(f["contract"]["text"], "Contract No. 2023-114")
        self.assertIn("one-year renewal", f["term"]["text"])
        self.assertIn("December 31, 2026", f["current"]["text"])
        self.assertNotIn("intends to exercise", f["current"]["text"])
        self.assertIn("CPI", f["pricing"]["text"])

    def test_notice_drafts_type_block_and_dates(self):
        upd, notes = briefing.fields_for({}, self.acc, [])
        self.assertEqual(upd["Solicitation_Type"], "Renewal Notice")
        self.assertTrue(upd["Scope_of_Work"].startswith(services.RENEWAL_HEAD))
        self.assertIn("• Existing contract: Contract No. 2023-114 (renewal.docx)", upd["Scope_of_Work"])
        self.assertEqual(upd["Proposal_Due"], "2026-11-02T17:00:00-06:00")      # the answer the agency needs, CST
        self.assertEqual(upd["Anticipated_Start"], "2027-01-01")
        self.assertEqual(notes, [])

    def test_existing_type_and_scope_are_never_overwritten(self):
        upd, notes = briefing.fields_for({"Solicitation_Type": "RFP", "Scope_of_Work": "Beau wrote this"}, self.acc, [])
        self.assertNotIn("Solicitation_Type", upd)
        self.assertNotIn("Scope_of_Work", upd)
        self.assertTrue(any("Solicitation Type is RFP" in n for n in notes))
        self.assertTrue(any("Renewal details found" in n for n in notes))

    def test_renewal_options_inside_an_rfp_are_not_a_notice(self):
        rfp = "Request for Proposals 2026-14\nElectronic Monitoring Services\nThe initial term is three years with two one-year renewal options.\n"
        acc = briefing.merge(briefing.new_findings(), briefing.scan("rfp.txt", text(rfp)))
        self.assertFalse(acc["renewal"]["notice"])
        self.assertNotIn("Solicitation_Type", briefing.fields_for({}, acc, [])[0])

    def test_renewal_form_inside_an_rfp_packet_is_not_a_notice(self):
        acc = briefing.new_findings()
        briefing.merge(acc, briefing.scan("rfp.txt", text("Request for Proposals 2026-14\nScope\nThe Contractor shall provide GPS monitoring.\n")))
        briefing.merge(acc, briefing.scan("pricing.txt", text("Renewal Option Pricing Form\nYear 1 renewal price: $1.00\n")))
        self.assertFalse(acc["renewal"]["notice"])
        # ... whichever order the documents are read in
        acc = briefing.new_findings()
        briefing.merge(acc, briefing.scan("pricing.txt", text("Renewal Option Pricing Form\nYear 1 renewal price: $1.00\n")))
        briefing.merge(acc, briefing.scan("rfp.txt", text("Request for Proposals 2026-14\nScope\n")))
        self.assertFalse(acc["renewal"]["notice"])

    def test_notice_with_no_details_still_flagged(self):
        acc = briefing.merge(briefing.new_findings(), briefing.scan("n.txt", text("Contract Renewal\nSee attached.\n")))
        self.assertIn("Read the notice", services.renewal_block(acc["renewal"]))

    def test_block_split_roundtrip(self):
        block = services.renewal_block(self.acc["renewal"])
        lines, rest = services.split_renewal_block(block + "\n\nScope text here.")
        self.assertEqual(lines[0].split(" (")[0], services.RENEWAL_HEAD)
        self.assertEqual(rest, "Scope text here.")
        self.assertEqual(services.split_renewal_block("plain"), ([], "plain"))


class RenewalBriefing(unittest.TestCase):
    def _sol(self, **kw):
        acc = briefing.merge(briefing.new_findings(), briefing.scan("renewal.docx", text(NOTICE)))
        sol = {"Name": "County EM renewal", "Key_Dates_Verified": False, "Incumbent": "JusticePoint"}
        sol.update(briefing.fields_for(sol, acc, [])[0])
        sol.update(kw)
        return sol

    def test_document_has_renewal_callout_and_services(self):
        sol = self._sol()
        txt = extract.docx_text(briefdoc.build(sol, []))
        for needle in ("Contract renewal", "This is a renewal of an existing contract", "Contract No. 2023-114",
                       "RENEWAL RESPONSE DUE", "Renewal term starts", "Renewal Notice",
                       "A renewal notice is not scored", "Services required"):
            self.assertIn(needle, txt)
        self.assertEqual(txt.count("Contract No. 2023-114"), 1, "the details are shown once, in the callout")
        self.assertNotIn(services.RENEWAL_HEAD, txt)
        self.assertNotIn("PROPOSAL DUE", txt)
        self.assertNotIn("Scoring: points and weights", txt)

    def test_services_section_from_record_fields(self):
        sol = {"Name": "x", "Service_Lines": ["EM GPS", "Pretrial"], "Service_Type_Summary": "• EM GPS: \"GPS monitoring\" (rfp.pdf, p.3)"}
        txt = extract.docx_text(briefdoc.build(sol, []))
        self.assertIn("Service lines: EM GPS, Pretrial", txt)
        self.assertIn("GPS monitoring", txt)
        self.assertNotIn("Read from the text on this record", txt)

    def test_services_section_falls_back_to_text_on_the_record(self):
        sol = {"Name": "x", "Scope_of_Work": "The Contractor shall provide GPS monitoring for 120 offenders."}
        txt = extract.docx_text(briefdoc.build(sol, [{"Requirement_Text": "Provide continuous alcohol monitoring units.", "Mandatory": True}]))
        self.assertIn("Service lines: EM GPS, EM Alcohol / CAM", txt)
        self.assertIn("Read from the text on this record", txt)

    def test_services_section_empty_record(self):
        txt = extract.docx_text(briefdoc.build({"Name": "x"}, []))
        self.assertIn("Services required", txt)
        self.assertIn("No services were found by rule", txt)
        self.assertNotIn("Contract renewal", txt)

    def test_rfp_document_unchanged_by_renewal_work(self):
        txt = extract.docx_text(briefdoc.build({"Name": "x", "Solicitation_Type": "RFP", "Proposal_Due": "2026-10-20T14:00:00-04:00"}, []))
        self.assertIn("PROPOSAL DUE", txt)
        self.assertNotIn("RENEWAL RESPONSE DUE", txt)
        self.assertIn("No point values were found", txt)


class IngestRun(unittest.TestCase):
    def test_renewal_notice_ingested_end_to_end(self):
        crm = FakeCrm({"n1": ("Renewal Notice.txt", NOTICE.encode())})
        msg = ingest.run(crm, "1", budget_s=600)
        self.assertEqual(crm.sol["Solicitation_Type"], "Renewal Notice")
        self.assertIn("RENEWAL DETAILS", crm.sol["Scope_of_Work"])
        self.assertEqual(crm.sol["Proposal_Due"], "2026-11-02T17:00:00-06:00")
        self.assertIn("Solicitation Type", msg)

    def test_services_ingested_end_to_end(self):
        crm = FakeCrm({"r1": ("rfp.txt", RFP.encode())})
        crm.sol["Solicitation_Type"] = "RFP"
        msg = ingest.run(crm, "1", budget_s=600)
        self.assertEqual(crm.sol["Service_Lines"][:3], ["EM GPS", "EM RF / RB", "EM Alcohol / CAM"])
        self.assertIn("Services required", crm.sol["Service_Type_Summary"])
        self.assertEqual(crm.sol["Solicitation_Type"], "RFP")
        self.assertIn("Service Lines", msg)

    def test_no_ai(self):
        with open(services.__file__, encoding="utf-8") as fh:
            src = fh.read().lower()
        for bad in ("anthropic", "openai", "urllib", "requests", "import socket"):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()

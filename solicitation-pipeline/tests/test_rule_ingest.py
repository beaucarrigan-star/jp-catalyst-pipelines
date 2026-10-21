import os
import pathlib
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solpipe.rule_ingest import parse_pages, map_table_rows, parse_table  # noqa: E402

PAGE1 = """3.4 Minimum Qualifications
3.4.1 The Contractor shall maintain commercial general liability insurance of at least $1,000,000 per occurrence.
3.4.2 Offerors must provide three references from similar contracts within the last five years.
The City welcomes proposals from all vendors.
3.5 Equipment
3.5.1 Each GPS device shall have a battery life of at least 40 hours and tamper alerts.
Vendors should describe their training approach."""


class RuleIngest(unittest.TestCase):
    def test_extracts_mandatory_and_soft(self):
        r = parse_pages([PAGE1])
        texts = [x["Requirement_Text"] for x in r["rows"]]
        self.assertEqual(len(texts), 4)
        self.assertFalse(r["needs_manual"])
        ins = r["rows"][0]
        self.assertEqual((ins["Mandatory"], ins["Category"], ins["Source_Reference"], ins["Page_PDF"]),
                         (True, "Insurance", "3.4.1", "1"))
        self.assertEqual(ins["Section_Group"], "3.4 Minimum Qualifications")
        self.assertEqual(r["rows"][2]["Category"], "Technology / Device")
        self.assertFalse(r["rows"][3]["Mandatory"])
        self.assertTrue(all(x["Our_Position"] == "Unverified" for x in r["rows"]))

    def test_unpaged_text_has_no_page(self):
        self.assertTrue(all(x["Page_PDF"] is None for x in parse_pages([PAGE1], paged=False)["rows"]))

    def test_scanned_flagged(self):
        r = parse_pages(["", " ", "\x0c"])
        self.assertTrue(r["needs_manual"])
        self.assertEqual(r["rows"], [])

    def test_no_duplicates(self):
        self.assertEqual(len(parse_pages([PAGE1, PAGE1])["rows"]), 4)

    def test_table_map(self):
        rows = [{"Line": "K-12", "Spec": "Device weight under 6 oz", "Req": "M"},
                {"Line": "K-13", "Spec": "Color screen", "Req": "D"}]
        out = map_table_rows(rows, {"Requirement_Text": "Spec", "Source_Reference": "Line", "Mandatory": "Req"})
        self.assertEqual([o["Mandatory"] for o in out], [True, False])
        self.assertEqual(out[0]["Source_Reference"], "K-12")

    def test_table_without_ref_column_uses_sheet_row(self):
        grid = [("1", ["Requirement Description", "Notes"]), ("2", ["Vendor shall provide 24/7 support", ""]),
                ("3", ["ok", ""])]
        out = parse_table(grid, "Specs")
        self.assertEqual([(o["Source_Reference"], o["Mandatory"]) for o in out], [("Specs row 2", True)])
        self.assertIsNone(parse_table([("1", ["Pricing", "Total"])], "Price"))

    def test_no_ai(self):
        src = (pathlib.Path(__file__).parent.parent / "solpipe" / "rule_ingest.py").read_text()
        for bad in ["gemini", "openai", "anthropic", "cohere", "urllib", "requests", "LLM_API_KEY"]:
            self.assertIsNone(re.search(bad, src, re.I), bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)

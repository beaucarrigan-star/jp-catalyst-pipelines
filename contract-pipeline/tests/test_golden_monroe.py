# Monroe County, OH (Contract 6715134000005360018) -- JusticePoint-template customer agreement, the first
# real (non-synthetic) customer contract through the pipeline, 27 Sep 2026. The signed PDF is a scan; this
# fixture is the OCR text (ocrmypdf) of "Monroe County, OH EM (2024).pdf". Expected values match CRM.
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "con_worker"))
from packet_extract import PacketDoc, extract_packet

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "monroe_oh.txt")


def _run():
    t = open(FIX, encoding="utf-8").read()
    return extract_packet([PacketDoc(name="Monroe County, OH EM (2024).pdf", text=t)], "Customer",
                          contract_has_account=True, packet_name="Monroe County, OH EM (2024).pdf")


def test_monroe_terms():
    v = {k: f.value for k, f in _run().terms.items()}
    assert v["Contract_Start_Date"] == "2024-09-17"
    assert v["Contract_End_Date"] == "2026-09-30"
    assert v["Auto_Renews"] == "Yes"
    assert v["Renewal_Options_Left"] == 2          # "two, 1-year extensions"
    assert v["Renewal_Option_Length_Mo"] == 12     # not the "3 months written notice"
    assert v["Notice_Period_Days"] == 90           # "not less than 3 months written notice"
    assert v["Termination_Convenience_Days"] == 90 # "terminated by either party ... ninety (90) days in advance"
    assert v["Payment_Terms_Days"] == 30
    assert v["Invoice_Cadence"] == "Monthly"


def test_monroe_rates_and_spares():
    r = _run()
    daily = [(x.device, x.amount) for x in r.rate_rows]
    assert daily == [("GPS", 4.5)]                 # the $800 replacement cost is not a daily rate
    assert r.pricing["spare_pct"] == 20 and r.pricing["spare_fee"] == 1.5   # "shelf units ... exceeds 20%"

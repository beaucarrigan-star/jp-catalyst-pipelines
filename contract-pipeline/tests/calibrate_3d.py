# calibrate_3d.py -- prints what the clause finder surfaces on each golden packet. Not a test; run it
# after any change to clause_finder.py thresholds or dictionary:  python tests/calibrate_3d.py
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "con_worker"))
sys.path.insert(0, HERE)

from test_golden_3b_3c import ALLIED_STD_PACKET, doc  # noqa: E402
from packet_extract import extract_packet  # noqa: E402
from clause_finder import find_clauses  # noqa: E402

PACKETS = {
    "1 Allied standard": (ALLIED_STD_PACKET, "Vendor", False),
    "2 Allied Sheboygan": ([doc("allied_add22", "Addendum 22 - Sheboygan.pdf"),
                            doc("allied_add23", "Addendum 23 w Exhibit 2-Justice Point for Sheboygan County Sheriff.pdf")], "Vendor", True),
    "3 Milwaukee (synthetic)": ([doc("SYNTHETIC_milwaukee", "Milwaukee County EM Agreement 2024.pdf", "Base agreement")], "Customer", True),
    "4 Hope School (synthetic + real amend)": ([doc("SYNTHETIC_hope_school", "Hope School EM Contract 2025.pdf", "Base agreement"),
                                              doc("hope_school_amend", "Amendment 1 Hope School 2026-01-05.pdf")], "Customer", True),
    "5 SCRAM (real)": ([doc("ams_master", "AMS_Master_Service_Partner_Agreement_20240110_20270109.pdf"),
                        doc("ams_schedule", "AMS Product and Service Schedule.pdf")], "Vendor", False),
    "6 Ramsey (synthetic)": ([doc("SYNTHETIC_ramsey", "Ramsey County PSA 2025.pdf", "Base agreement")], "Customer", True),
}


def run(verbose=True):
    out = {}
    for name, (docs, ct, has_acct) in PACKETS.items():
        res = extract_packet(docs, ct, contract_has_account=has_acct)
        props, dropped, notes = find_clauses(res.docs, ct)
        out[name] = (props, dropped, notes)
        if verbose:
            print("=" * 100)
            print(name, "--", len(props), "proposed,", dropped, "dropped", ("; " + "; ".join(notes)) if notes else "")
            for p in props:
                print("  [{}] {:>2} {:<14} {:<8} {}".format(p.confidence[0], p.score, p.clause_type, p.obligor, p.title))
                print("       {} | cadence={!r}".format(p.source, p.cadence))
                print("       " + p.text[:230].replace("\n", " ") + ("..." if len(p.text) > 230 else ""))
    return out


if __name__ == "__main__":
    run()

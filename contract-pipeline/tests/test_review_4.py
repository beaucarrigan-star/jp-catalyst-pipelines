# Phase 4 (27 Sep 2026): Ingest_Review JSON for the Contract Review widget.
# Run: python -m pytest tests -q  (from contract-pipeline/)
import json
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "con_worker"))

import review_payload as rp  # noqa: E402
from test_worker_e2e import FakeZoho, _zip  # noqa: E402


def _run_scram(monkeypatch, contract=None):
    import main
    from test_golden_3b_3c import EXISTING_SCRAM
    contract = contract or {"id": "c2", "Counterparty_Type": "Vendor", "Account": None, "Ingest_Status": None,
                            "Rate_Lines": [dict(r) for r in EXISTING_SCRAM]}
    fake = FakeZoho(contract, _zip([
        ("Justice Point_Master SP Agreement_Expires 20270109.pdf", "ams_master"),
        ("Justice Point_Product and Service Schedule_Expires 20270109.pdf", "ams_schedule"),
    ]))
    monkeypatch.setattr(main, "zc", fake)
    main._ingest_contract(contract["id"])
    return contract, fake


def test_review_json_written(monkeypatch):
    contract, _ = _run_scram(monkeypatch)
    rv = json.loads(contract["Ingest_Review"])
    assert rv["v"] == 1 and rv["packet"] and len(contract["Ingest_Review"]) <= rp.MAX_LEN
    by = {f["f"]: f for f in rv["fields"]}
    end = by["Contract_End_Date"]
    assert end["v"] == "2027-01-09" and end["w"] and end["src"] and end["ev"]   # value, citation, snippet
    assert rv["rates"] and all(r["k"].count("|") == 3 and r["src"] for r in rv["rates"])
    cam = [r for r in rv["rates"] if r["k"].startswith("CAM|0||")]
    assert cam, [r["k"] for r in rv["rates"]]
    assert rv["decisions"] == {"fields": {}}


def test_decisions_survive_and_are_obeyed(monkeypatch):
    contract, _ = _run_scram(monkeypatch)
    rv = json.loads(contract["Ingest_Review"])
    # a person rejects Notice_Period_Days (clears it) and edits the end date in the widget
    rv["decisions"]["fields"] = {
        "Notice_Period_Days": {"s": "rejected", "by": "Beau", "at": "2026-09-27"},
        "Contract_End_Date": {"s": "edited", "v": "2027-06-30", "by": "Beau", "at": "2026-09-27"},
    }
    contract["Ingest_Review"] = json.dumps(rv)
    contract["Notice_Period_Days"] = None
    contract["Contract_End_Date"] = "2027-06-30"
    contract["Packet_Hash"] = "changed"
    contract, fake = _run_scram(monkeypatch, contract)
    assert contract["Notice_Period_Days"] is None
    assert contract["Contract_End_Date"] == "2027-06-30"
    assert "Rejected in review, not re-written: Notice_Period_Days" in fake.notes[-1]
    assert json.loads(contract["Ingest_Review"])["decisions"]["fields"]["Contract_End_Date"]["s"] == "edited"


def test_apply_decisions_same_value_is_silent():
    kept, notes = rp.apply_decisions({"A": 1, "B": 2}, {"A": {"s": "confirmed", "v": 1}})
    assert kept == {"B": 2} and notes == []


def test_fit_under_cap():
    big = {"v": 1, "fields": [{"f": "X%d" % i, "ev": "word " * 300} for i in range(80)],
           "rates": [{"k": "a|0||", "ev": "x " * 200} for _ in range(80)], "decisions": {"fields": {}}}
    assert len(rp._fit(big)) <= rp.MAX_LEN


def test_rate_key_matches_writeback():
    from writeback import _row_key
    row = {"Rate_Device": "GPS", "Tier_Min_Units": 401, "Rate_Terms": "", "Effective_Date": "2026-02-01"}
    k = _row_key(row)
    assert rp.rate_key("GPS", 401, "", "2026-02-01") == "{}|{}|{}|{}".format(*k)


# ---- 27 Sep 2026 fix: first live run failed on a single-PDF contract (Monroe County, OH) ----

def test_single_pdf_attachment_is_a_packet(monkeypatch):
    import main
    from test_worker_e2e import _pdf
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "fixtures", "ams_master.txt"), encoding="utf-8") as f:
        pdf = _pdf(f.read())
    contract = {"id": "c7", "Counterparty_Type": "Vendor", "Account": None, "Ingest_Status": None, "Rate_Lines": []}
    fake = FakeZoho(contract, b"")
    fake.list_attachments = lambda m, r: [{"id": "p1", "File_Name": "Justice Point_Master SP Agreement_Expires 20270109.pdf"},
                                          {"id": "x1", "File_Name": "notes.png"}]
    fake.download_attachment = lambda m, r, a: pdf
    monkeypatch.setattr(main, "zc", fake)
    main._ingest_contract("c7")
    assert contract["Ingest_Status"] == "Needs Review"
    assert contract["Contract_End_Date"] == "2027-01-09"
    assert json.loads(contract["Ingest_Review"])["packet"].endswith(".pdf")
    first_hash = contract["Packet_Hash"]
    main._ingest_contract("c7")                       # same PDF again: deterministic bundle -> no-op
    assert "already read earlier" in fake.notes[-1] and contract["Packet_Hash"] == first_hash


def test_no_readable_attachment_fails_with_note(monkeypatch):
    import main
    contract = {"id": "c8", "Counterparty_Type": "Customer", "Account": None, "Ingest_Status": None, "Rate_Lines": []}
    fake = FakeZoho(contract, b"")
    fake.list_attachments = lambda m, r: [{"id": "x1", "File_Name": "photo.png"}]
    monkeypatch.setattr(main, "zc", fake)
    main._ingest_contract("c8")
    assert contract["Ingest_Status"] == "Failed" and "No readable attachment" in fake.notes[-1]


def test_list_attachments_sends_fields(monkeypatch):
    os.environ.setdefault("ZOHO_REFRESH_TOKEN", "x"); os.environ.setdefault("ZOHO_CLIENT_ID", "x")
    os.environ.setdefault("ZOHO_CLIENT_SECRET", "x")
    import importlib.util
    spec = importlib.util.spec_from_file_location("zc_real", os.path.join(HERE, "..", "con_worker", "zoho_client.py"))
    zc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(zc)
    seen = {}

    class R:
        status_code = 204

    def fake_get(url, headers=None, params=None, timeout=None):
        seen["params"] = params
        return R()
    monkeypatch.setattr(zc, "get_access_token", lambda: "t")
    monkeypatch.setattr(zc.requests, "get", fake_get)
    assert zc.list_attachments("Contracts", "1") == []
    assert "File_Name" in seen["params"]["fields"]


def test_coql_never_chains_three_conditions(monkeypatch):
    """Live 400 on 27 Sep: COQL rejects `a and b and c` at one level; every query the worker sends
    must group conditions two at a time."""
    import re
    import main
    from test_golden_3b_3c import EXISTING_SCRAM
    sent = []
    contract = {"id": "c9", "Counterparty_Type": "Vendor", "Account": None, "Ingest_Status": None,
                "Rate_Lines": [dict(r) for r in EXISTING_SCRAM]}
    fake = FakeZoho(contract, _zip([("Justice Point_Master SP Agreement_Expires 20270109.pdf", "ams_master")]))
    real = fake.coql
    fake.coql = lambda q: (sent.append(q), real(q))[1]
    monkeypatch.setattr(main, "zc", fake)
    main._ingest_contract("c9")
    assert sent
    for q in sent:
        where = q.split(" where ", 1)[1].rsplit(" limit ", 1)[0]
        depth, level_ands = 0, {}
        for tok in re.findall(r"\(|\)|\band\b|\bor\b|'[^']*'", where):
            if tok == "(":
                depth += 1
            elif tok == ")":
                depth -= 1
            elif tok in ("and", "or"):
                level_ands[depth] = level_ands.get(depth, 0) + 1
        assert max(level_ands.values() or [0]) <= 1, q

# 9 Oct 2026: vendor vs customer separation -- blank Counterparty_Type stops the ingest, the Vendors
# entry point creates a Vendor contract, the Note names the profile.
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "con_worker"))
sys.path.insert(0, HERE)

import main  # noqa: E402
from test_worker_e2e import FakeZoho, _zip  # noqa: E402

AMS = [("Justice Point_Master SP Agreement_Expires 20270109.pdf", "ams_master")]


def test_blank_counterparty_type_stops_with_note(monkeypatch):
    contract = {"id": "c1", "Counterparty_Type": None, "Account": None, "Ingest_Status": None, "Rate_Lines": []}
    fake = FakeZoho(contract, _zip(AMS))
    monkeypatch.setattr(main, "zc", fake)
    main._ingest_contract("c1")
    assert contract["Ingest_Status"] == "Failed"
    assert "Counterparty_Type is blank" in fake.notes[-1]
    assert not contract.get("Packet_Hash") and fake.docs == []   # nothing read, so a rerun after fixing works


def test_note_names_the_profile(monkeypatch):
    contract = {"id": "c2", "Counterparty_Type": "Vendor", "Account": None, "Ingest_Status": None, "Rate_Lines": []}
    fake = FakeZoho(contract, _zip(AMS))
    monkeypatch.setattr(main, "zc", fake)
    main._ingest_contract("c2")
    assert fake.notes[-1].startswith("Read as a Vendor contract")


def test_vendor_entry_point_creates_vendor_contract(monkeypatch):
    created, moved = [], []

    class Fake:
        def get_record(self, module, rid, fields=None):
            assert module == "Vendors" and fields == ["Vendor_Name"]
            return {"Vendor_Name": "Buddi"}

        def create_record(self, module, data):
            created.append((module, data))
            return {"details": {"id": "new1"}}

        def list_attachments(self, module, rid):
            return [{"id": "z", "File_Name": "buddi.zip"}, {"id": "p", "File_Name": "logo.png"}]

        def download_attachment(self, module, rid, aid):
            return b"zip"

        def upload_attachment(self, module, rid, name, raw):
            moved.append((module, rid, name))

        def delete_attachment(self, module, rid, aid):
            moved.append(("deleted", module, aid))

    monkeypatch.setattr(main, "zc", Fake())
    monkeypatch.setattr(main, "_ingest_contract", lambda cid: moved.append(("ingest", cid)))
    main.run("555", "new_vendor_contract_from_packet")
    module, data = created[0]
    assert module == "Contracts" and data["Counterparty_Type"] == "Vendor"
    assert data["Vendor"] == {"id": "555"} and "Account" not in data
    assert data["Name"] == "Buddi - vendor pricing from packet"
    assert moved == [("Contracts", "new1", "buddi.zip"), ("deleted", "Vendors", "z"), ("ingest", "new1")]


def test_account_entry_point_stays_customer(monkeypatch):
    created = []

    class Fake:
        def get_record(self, module, rid, fields=None):
            return {"Account_Name": "Adams Co. HHS"}

        def create_record(self, module, data):
            created.append(data)
            return {"details": {"id": "n"}}

        def list_attachments(self, module, rid):
            return []

    monkeypatch.setattr(main, "zc", Fake())
    monkeypatch.setattr(main, "_ingest_contract", lambda cid: None)
    main.run("777", "new_contract_from_packet")
    assert created[0]["Counterparty_Type"] == "Customer" and created[0]["Account"] == {"id": "777"}


def test_con_api_matches_action_to_module():
    # Read con_api's tables from its source (no import): con_api needs flask, which the deploy machine's
    # test Python doesn't have -- and the deploy gate must not depend on it.
    import ast
    with open(os.path.join(HERE, "..", "con_api", "main.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    consts = {t.id: ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name) and t.id in ("ACTIONS", "ACTION_MODULE")}
    assert consts["ACTION_MODULE"]["new_vendor_contract_from_packet"] == "Vendors"
    assert consts["ACTION_MODULE"]["new_contract_from_packet"] == "Accounts"
    assert set(consts["ACTION_MODULE"]) == set(consts["ACTIONS"])


def test_one_time_charges_are_not_daily_rates():
    # Burnett/Waushara/Price (9 Oct 2026): lost-device prices were written as $/day and superseded the real rate
    import rate_extractor as rx
    res = rx.DocRates(lines=[
        rx.RateProposal(device="GPS", amount=4.5, unit="day", tier_min=None, tier_max=None, effective_date=None,
                        file_name="Burnett.pdf", page=2, label="GPS"),
        rx.RateProposal(device="GPS", amount=850.0, unit="day", tier_min=None, tier_max=None, effective_date=None,
                        file_name="Burnett.pdf", page=5, label="replacement")])
    rx._drop_one_time_charges(res)
    assert [ln.amount for ln in res.lines] == [4.5]
    assert any("one-time charge" in n and "850.00" in n for n in res.notes)

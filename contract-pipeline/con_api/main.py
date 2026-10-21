# con_api -- Advanced I/O function (type "advancedio"), contract-pipeline Catalyst project.
# Same shape as the live sol_api (claude/solicitation-pipeline-catalyst-live-2026-09-25.md): Catalyst's
# python_3_9 Advanced I/O runtime calls handler(request) with a Flask Request and expects a Flask
# response back. It checks the shared-secret token, then hands the real work to con_worker as a
# background job so a big packet never runs into the request ceiling.
#
# Routes (base path /server/con_api/):
#   GET  /health   -> "contract ingest pipeline 1.0.0"
#   POST /ingest   -> body {"module": "Contracts"|"Accounts"|"Vendors", "recordId": "...", "action": "..."}
#                     action is "ingest_contract" (packet already on a Contract) or
#                     "new_contract_from_packet" (packet on an Account; con_worker creates a Customer
#                     Contract first and moves the zip onto it) or "new_vendor_contract_from_packet"
#                     (module "Vendors"; same, but a Counterparty_Type = Vendor Contract). Returns 202 with the job name immediately;
#                     con_worker posts the Note when it finishes.
#
# contract_pipeline_call (the standalone Deluge helper) is the only caller. It sends CON_PIPELINE_TOKEN
# in the X-Con-Token header; requests without the right token get 401.

import hmac
import logging
import os

from flask import jsonify, make_response

VERSION = "1.0.0"
JOBPOOL = "conjobs"          # console: Job Scheduling > Job Pool, id 31190000000040063, 1 GB
WORKER = "con_worker"
ACTIONS = ("ingest_contract", "new_contract_from_packet", "new_vendor_contract_from_packet")
# 9 Oct 2026: each action is only valid from its own module (vendor vs customer separation).
ACTION_MODULE = {"ingest_contract": "Contracts", "new_contract_from_packet": "Accounts",
                 "new_vendor_contract_from_packet": "Vendors"}
log = logging.getLogger()


def _reply(code, **body):
    return make_response(jsonify(body), code)


def handler(request):
    # Catalyst may pass the path with or without the /server/con_api prefix; match on the last segment.
    tail = (request.path or "/").rstrip("/").rsplit("/", 1)[-1]
    if tail in ("", "con_api", "health") and request.method == "GET":
        return make_response("contract ingest pipeline " + VERSION, 200)
    if tail != "ingest" or request.method != "POST":
        return _reply(404, status="error", message="not found")

    expected = os.environ.get("CON_PIPELINE_TOKEN", "").strip()
    got = (request.headers.get("X-Con-Token") or request.headers.get("x-con-token") or "").strip()
    if not expected or not hmac.compare_digest(expected, got):
        # Say WHY without revealing either value: which side is missing, or that they differ.
        reason = ("CON_PIPELINE_TOKEN is not set on con_api" if not expected else
                  "no X-Con-Token header reached con_api" if not got else
                  "token mismatch (expected {} chars, got {})".format(len(expected), len(got)))
        return _reply(401, status="error", message="unauthorized: " + reason)

    body = request.get_json(silent=True) or {}
    module = str(body.get("module", "")).strip()
    record_id = str(body.get("recordId", "")).strip()
    action = str(body.get("action", "")).strip()
    if action not in ACTIONS or ACTION_MODULE[action] != module or not record_id.isdigit():
        return _reply(400, status="error", message="module, numeric recordId and action are required: "
                                                   "Contracts/ingest_contract, Accounts/new_contract_from_packet "
                                                   "or Vendors/new_vendor_contract_from_packet")

    try:
        import zcatalyst_sdk
        app = zcatalyst_sdk.initialize()
        job_name = "ing_" + record_id[-14:]  # Catalyst caps job names at 20 characters
        app.job_scheduling().job.submit_job({
            "job_name": job_name,
            "jobpool_name": JOBPOOL,
            "target_type": "Function",
            "target_name": WORKER,
            "params": {"module": module, "recordId": record_id, "action": action},
        })
    except Exception as e:  # noqa: BLE001 -- surface every failure to the button
        log.exception("submit failed")
        return _reply(500, status="error", message="could not queue the ingest: " + str(e)[:600])
    return _reply(202, status="queued", job=job_name,
                  message="Reading the contract packet in the background. This usually takes 1-5 minutes; "
                          "the result appears as a Note on the Contract. Refresh the page to see it.")

# Solicitation pipeline (Python)

Replaces the four Deluge solicitation functions with Python that runs in Zoho Catalyst, keeping the
existing CRM buttons. **Version 2.0 (Sep 25, 2026): no AI tokens anywhere.** Ingest is rule-based and
Re-score runs the deterministic scoring engine. Nothing in this package calls a model, and no model key
is read, stored or deployed.

| Button | Action | Where it runs | What it writes |
| --- | --- | --- | --- |
| Ingest Requirements (Solicitation) | `ingest` | Catalyst job, up to 15 min per press | Draft Solicitation Requirements (all Unverified); Ingest Queue, Ingested Attachments, Extraction Notes, Parse Date |
| Seed Scorecard (Bid Scenario) | `seed` | inline, seconds | 17 Scenario Scores with weights and anchors |
| Generate Spec Matrix (Bid Scenario) | `matrix` | Catalyst job | Spec Assessments, requirement x device |
| Re-score (Bid Scenario) | `score` | inline, seconds | Weighted Score, Score Range, Coverage %, Rule Fired, Cat A-F, gates, Recommendation + Reason (with run key); parent Best Scenario |
| daily schedule (`solicitation_milestone_alerts`) | `milestones all` | inline, seconds | Days To Deadline, Deadline Alert Stage, Next Milestone + date on every open Solicitation |

Background actions post their result as a **Note** on the record. Re-score also creates a High task for the
scenario owner when the Recommendation changes (old -> new): Zoho's workflow API cannot trigger on a field change.

## Ingest (rule-based)

- **PDF**: the text layer is read page by page (pdfminer.six). Sentences with shall / must / required /
  mandatory become Mandatory rows; should / preferred become non-mandatory rows. Numbered clauses give
  the Source Reference (e.g. `rfp.pdf, §3.4.1, p.14`), numbered headings give the Section Group, and the
  PDF page goes into Page (PDF). Category comes from keyword rules; anything unmatched is Other.
- **Scanned PDFs** have no text layer. They are marked `MANUAL` in the Ingest Queue and named in the
  result Note - enter those requirements by hand. There is no OCR.
- **XLSX (Form K and similar matrices)**: the header row is found by rule (a Requirement / Specification /
  Description column, plus optional Item/Ref, Mandatory (M/D) and Section columns). One row per line;
  single-cell rows become the running Section Group. A sheet with no recognisable header is read as text.
- **DOCX / TXT / CSV**: same sentence rules as PDF, no page numbers.
- **ZIP**: for packets over Zoho's attachment limit. Every PDF, XLSX, DOCX, TXT or CSV inside (any folder)
  is read as if attached on its own, and rows cite `packet.zip > file.pdf`. Not read: zips inside the zip,
  password-protected files, other file types (each named in the Note). Limits: 200 files, 60 MB per file,
  250 MB unzipped in total.
- Every row lands as **Our Position = Unverified** and Priority = Unspecified. Rules find candidate
  obligations; a person confirms wording, Mandatory, Category and page before scoring relies on them.
- One press reads every unread attachment. Clearing an attachment id from Ingested Attachments re-reads
  it; dedupe on requirement text means a re-read creates nothing new. Every CRM write is checked.

## Re-score (scoring engine)

- The scorecard follows the scenario's **Lead Organization**:
  - **JusticePoint** - Workbook-v1, Approved: weights from `SOLICITATION_SCORING_WEIGHTS`, Go 75 /
    Conditional 55 from the threshold variables, K1-K8. Charlotte reproduces exactly.
  - **Sirona Recovery / TDP** - Handoff-v1, **Draft** (80 / 65 / 90% coverage), starting from the 17 workbook
    criteria. A Draft scorecard shows the score range but always recommends *Needs review* (rule 1).
    To set or approve one without a code change, create a CRM Variable `SOLICITATION_SCORECARD_SIRONA`
    or `SOLICITATION_SCORECARD_TDP` holding the scorecard JSON (shape in `solpipe/scorecards.py`) with
    `"approval_state": "Approved"`.
  - Blank Lead Organization is scored as JusticePoint.
- A blank criterion is **Unknown**, not zero: the score becomes a range (low = unknowns at 0, high = at 5)
  with a Coverage %. If the range crosses a band, the answer is *Needs review*. Answer Status
  *Not applicable* drops a criterion and renormalises the weights. Blank gates count as Unclear.
- Mandatory requirements on the parent Solicitation count: *Does not meet* with no Cure Owner or Cure
  Deadline is No-Go; requirements whose Subject Entity names another org are ignored.
- *Needs review* is not yet a value on the Recommendation picklist. Until it is added (Setup), the field
  shows *Not yet scored* and the reason text says Needs review.

## Run it from your own computer## Run it from your own computer (no Catalyst needed)

```
pip install pdfminer.six               # once: the PDF text reader (only ingest needs it)
python setup_token.py                  # once: writes %USERPROFILE%\.solpipe.env
python cli.py score  6715134000005548513 --dry-run    # Charlotte SUB: expect Conditional Go - 61.0
python cli.py ingest <solicitation id> --dry-run
```

Python 3.9+. Standard library only, apart from pdfminer.six for PDFs.

If an old `.solpipe.env` still has a `GEMINI_API_KEY=` line, delete that line; nothing reads it.

## Deploy to Catalyst

1. Done: Catalyst project **solicitation-pipeline** (id 31190000000033001) and Function job pool **soljobs**
   (512 MB) exist. (Catalyst refuses `_` in pool names.)
2. `python setup_token.py` (if not done).
3. `powershell -ExecutionPolicy Bypass -File .\deploy.ps1` - runs all four test files, vendors pdfminer.six as
   Linux wheels (Catalyst runs Linux; its `cryptography` dependency is compiled), logs in, and deploys both
   functions with the Zoho credentials injected from a build folder outside OneDrive. No AI key is deployed.
4. In CRM, Setup > Developer Hub > Functions: create the standalone function in
   `deluge/solicitation_pipeline_call.dg`, fill in the sol_api URL and `PIPELINE_TOKEN`, then replace the
   four button function bodies with `deluge/button_functions.dg` (back up the old bodies first).
5. Test on a scratch Solicitation: attach Form K + the RFP PDF, press Ingest Requirements.

## Tests

`python tests/test_pipeline.py`, `tests/test_ingest_flow.py`, `tests/test_scoring_engine.py`,
`tests/test_rule_ingest.py` (all pass on Python 3.9). The scoring tests reproduce the calibrated workbook
exactly: Charlotte PRIME 42.6 No-Go, PRIME+ 52.4 No-Go, SUB 61.0 Conditional Go, and the Sep 16
fixture's 100.0 Go. `test_no_ai_anywhere` fails the build if any model name, model endpoint or
`LLM_API_KEY` reference appears in the package. The ingest flow test builds PDFs with reportlab and skips
those parts if reportlab is not installed.

## Layout

```
solpipe/        the pipeline (config, crm, extract, rule_ingest, ingest, scoring_engine, scorecards, score,
                seed, matrix, milestones)
functions/      Catalyst sol_api (Advanced I/O) and sol_worker (Job); deploy copies solpipe into each
deluge/         CRM-side helper + button bodies
cli.py          local runner        setup_token.py   one-time credentials
deploy.ps1      Catalyst deploy     tests/           offline tests
```

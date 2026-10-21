"""Services required and contract-renewal facts, read by rule. No AI, no network (Beau, Oct 16 2026).

Two things the executive / RFP team briefing must carry on every Solicitation:

  Services required   which of our service lines the packet asks for (Service_Lines), the evidence for each
                      (Service_Type_Summary), and the duties the contractor is asked to perform
  Contract renewals   a renewal notice, option-year exercise or extension letter is a solicitation too:
                      Solicitation_Type = "Renewal Notice", with the existing contract, renewal term,
                      current term end, pricing and scope changes pulled from the notice

Rules that keep people's work safe (same as briefing.py):
  * only empty fields are drafted; every drafted line cites its file and page
  * a renewal is only called a notice when its title area says renewal and does not say RFP / RFQ / IFB
  * a mention of renewal options inside an ordinary RFP is not a renewal notice
"""
from __future__ import annotations

import re

SERVICE_LINES = ["EM GPS", "EM RF / RB", "EM Alcohol / CAM", "Pretrial", "Diversion", "Treatment",
                 "Case Management", "Consulting", "Other"]

SERVICE_RULES = [
    ("EM GPS", r"\bgps\b|global positioning|ankle (monitor|bracelet|device)|offender tracking"
               r"|active (electronic )?(monitoring|tracking)"),
    ("EM RF / RB", r"radio[- ]frequency|\bRF\b|\bRF\s*/\s*RB\b|home (detention|confinement|curfew)|house arrest"
                   r"|curfew monitoring|base station"),
    ("EM Alcohol / CAM", r"continuous alcohol|\bCAM\b|\bscram\b|transdermal|alcohol (monitoring|testing|detection)"
                         r"|breath ?alcohol|breathalyzer|remote alcohol"),
    # a department that uses the service ("Pre-Trial Release", "Community Supervision and Corrections") is not
    # a service line, so these need service wording
    ("Pretrial", r"pre-?trial (services?|supervision|monitoring|program|case management|assessment)"),
    ("Diversion", r"\bdiversion (program|services?|supervision|monitoring)"),
    ("Treatment", r"(substance (use|abuse)|behavioral health|mental health|drug|alcohol|cognitive[- ]behavioral|addiction)"
                  r"\s+(treatment|counseling|services?)|treatment (services?|program|provider)s?"
                  r"|counseling services|recovery (services|support)"),
    ("Case Management", r"case management|case manager|caseload|supervision services"),
    ("Consulting", r"consulting services|\bconsultant\b|technical assistance|program (design|evaluation) services"),
]
_COMPILED = [(n, re.compile(p, re.I)) for n, p in SERVICE_RULES]

PROVIDE_RX = re.compile(r"\b(shall|will|must|agrees? to|is required to|are required to|is to)\s+(?:also\s+)?"
                        r"(provide|furnish|supply|deliver|perform|offer|operate|supervise|monitor|install|maintain"
                        r"|administer|conduct|manage|be responsible for)\b", re.I)
# who is asked to do it: the contractor, not the County ("The County will provide a project resource")
CONTRACTOR_SUBJ = re.compile(r"\b(contractor|vendor|offeror|proposer|bidder|supplier|service provider"
                             r"|selected (firm|vendor)|successful (offeror|bidder|vendor))s?\b", re.I)
SERVICE_NOUN = re.compile(r"\b(monitor(ing|ed)?|supervis(ion|e)|tracking|install(ation)?|training|testing|reporting"
                          r"|alerts?|call cent(er|re)|customer (service|support)|24\s*/\s*7|screening|assessment"
                          r"|enrollment|reports?)\b", re.I)
SENT_SPLIT = re.compile(r"(?<=[.;])\s+(?=[A-Z(])")
LEADER = re.compile(r"\.{4,}|…{2,}")   # table-of-contents lines
NUM_HEAD = re.compile(r"^\s*(?:(?:\d+\.)+\d*|\d+\)|Section\s+\d+(?:\.\d+)*|ARTICLE\s+[IVX\d]+|[A-Z]\.)\s+\S", re.I)
MAX_DUTIES = 10


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _cite(e: dict) -> str:
    return f"{e['label']}, p.{e['page']}" if e.get("page") else str(e.get("label") or "")


def _is_heading(s: str) -> bool:
    """'3. SCOPE OF WORK', '3.1 Services Requested': a heading line must not be glued to the sentence under it."""
    if len(s) > 80 or s.endswith((".", ",", ";")):
        return False
    return (s.isupper() and len(s.split()) <= 10) or (bool(NUM_HEAD.match(s)) and len(s.split()) <= 7)


def _paragraphs(lines: list) -> list:
    """[(page, text)]: consecutive non-empty lines on one page joined into a paragraph."""
    out, buf, pg0 = [], [], None
    for pg, ln in lines:
        s = (ln or "").strip()
        if not s or LEADER.search(s):
            if buf:
                out.append((pg0, " ".join(buf)))
                buf = []
            continue
        if buf and pg != pg0:
            out.append((pg0, " ".join(buf)))
            buf = []
        if _is_heading(s):
            if buf:
                out.append((pg0, " ".join(buf)))
                buf = []
            continue
        if "\t" in s:      # a spreadsheet row: every cell is its own statement, never glued to the next row
            if buf:
                out.append((pg0, " ".join(buf)))
                buf = []
            out += [(pg, c.strip()) for c in s.split("\t") if c.strip()]
            continue
        if not buf:
            pg0 = pg
        buf.append(s)
    if buf:
        out.append((pg0, " ".join(buf)))
    return out


# ---------------- services ----------------

def new_services() -> dict:
    return {"lines": {}, "duties": []}


def find_services(lines: list, label: str) -> dict:
    """lines: [(page or None, text)] as briefing._lines_with_pages returns.
    {lines: {service line: {text, label, page}}, duties: [{text, label, page}]}.
    "Other" is never picked by rule: training, reporting and support duties are in every contract."""
    found: dict = {}
    for pg, ln in lines:
        s = _clean(ln)
        if not s or LEADER.search(s):
            continue
        for name, rx in _COMPILED:
            if name not in found and rx.search(s):
                found[name] = {"text": s[:200], "label": label, "page": pg}
    duties, seen = [], set()
    for pg, para in _paragraphs(lines):
        for sent in SENT_SPLIT.split(para):
            sent = _clean(sent)
            pm = PROVIDE_RX.search(sent)
            if not 25 <= len(sent) <= 400 or not pm or not CONTRACTOR_SUBJ.search(sent[:pm.start()]):
                continue
            if not (any(rx.search(sent) for _, rx in _COMPILED) or SERVICE_NOUN.search(sent)):
                continue
            key = sent.lower()[:120]
            if key in seen:
                continue
            seen.add(key)
            duties.append({"text": sent, "label": label, "page": pg})
            if len(duties) >= MAX_DUTIES:
                break
        if len(duties) >= MAX_DUTIES:
            break
    return {"lines": found, "duties": duties}


def merge_services(acc: dict, one: dict) -> dict:
    for name, e in one.get("lines", {}).items():
        acc["lines"].setdefault(name, e)      # first document and first hit win
    seen = {d["text"].lower()[:120] for d in acc["duties"]}
    for d in one.get("duties", []):
        if len(acc["duties"]) >= MAX_DUTIES:
            break
        if d["text"].lower()[:120] not in seen:
            seen.add(d["text"].lower()[:120])
            acc["duties"].append(d)
    return acc


def service_line_values(svc: dict) -> list:
    """Service_Lines picklist values found by rule, in picklist order."""
    return [n for n in SERVICE_LINES if n in svc["lines"]]


def services_summary(svc: dict) -> str:
    if not svc["lines"] and not svc["duties"]:
        return ""
    out = ["Services required (read by rule from the packet; check against the document):"]
    for name in SERVICE_LINES:
        e = svc["lines"].get(name)
        if e:
            out.append(f"• {name}: \"{e['text']}\" ({_cite(e)})")
    if svc["duties"]:
        out += ["", "Duties the contractor is asked to perform:"]
        out += [f"• {d['text']} ({_cite(d)})" for d in svc["duties"]]
    return "\n".join(out)[:30000]


# ---------------- renewals ----------------

RENEWAL_HEAD = "RENEWAL DETAILS"
TITLE_RENEW = re.compile(r"renewal|option (to renew|year|period)|exercise of (the )?option|contract extension"
                         r"|notice of (intent to )?(renew|extend|exercise)", re.I)
TITLE_NEW = re.compile(r"request for (proposals?|quotes?|quotations?|bids?|qualifications|information)"
                       r"|invitation (for|to) bid|\bRF[PQI]\b|\bIFB\b|\bITB\b|invitation to negotiate", re.I)
RENEW_MENTION = re.compile(r"\brenew(al|als|ed|ing)?\b|\boption (year|period|term)s?\b|\boption to (renew|extend)\b"
                           r"|\bexercis(e|ing) (of )?(the |its |an? )?option|\bcontract extension\b", re.I)
CONTRACT_NO = re.compile(r"\b(?:contract|agreement|purchase order|PO)\s*(?:no\.?|number|num\.?|#)\s*[:#]?\s*"
                         r"([A-Z0-9][A-Z0-9\-/.]{2,24})", re.I)
NUMWORD = r"(?:\d+|one|two|three|four|five|six)"
TERM_RX = re.compile(rf"(renewals?|options?|extensions?)\b[^.]{{0,60}}\b{NUMWORD}\b[^.]{{0,20}}(year|month)s?"
                     rf"|\b{NUMWORD}[- ]?(?:\(\d+\)\s*)?(year|month)s?[^.]{{0,40}}(renewals?|options?|extensions?)", re.I)
CURRENT_RX = re.compile(r"(current|existing|present)\s+(contract\s+)?(term|period|agreement)\b[^.]{0,60}"
                        r"(expires?|ends?|terminates?)|(contract|agreement|term)\s+(expires?|expiration|ends?)\b", re.I)
PRICING_RX = re.compile(r"(price|rate|fee|cost|pricing)s?\b[^.]{0,60}(increase|decrease|adjust|escalat|\bCPI\b|remain|unchanged|same)"
                        r"|(increase|escalat|adjust)\w*[^.]{0,40}(price|rate|\d\s*%)", re.I)
CHANGES_RX = re.compile(r"(amend|change|revis|modif)\w*[^.]{0,40}(scope|terms|services|specifications)", re.I)
DATE_NEAR = re.compile(r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
                       r"|\d{1,2}/\d{1,2}/\d{2,4}", re.I)
RENEWAL_FACTS = [("contract", "Existing contract"), ("term", "Renewal term"), ("current", "Current term"),
                 ("pricing", "Pricing"), ("changes", "Changes")]


def new_renewal() -> dict:
    # notice: some document is a renewal notice and none of the documents is an RFP / RFQ / IFB.
    # (A renewal-option pricing form inside an RFP packet must not turn the whole packet into a renewal.)
    return {"notice": False, "mentions": False, "facts": {}, "_renewal_doc": False, "_new_doc": False}


def _title_region(lines: list) -> str:
    out = []
    for _, ln in lines:
        if ln.strip():
            out.append(ln.strip())
        if len(out) >= 25:
            break
    return "\n".join(out)


def scan_renewal(lines: list, label: str) -> dict:
    """Is this document a renewal notice, and what does it say about the renewal?"""
    title = _title_region(lines)
    renewal_doc, new_doc = bool(TITLE_RENEW.search(title)), bool(TITLE_NEW.search(title))
    notice = renewal_doc and not new_doc
    mentions = any(RENEW_MENTION.search(ln) for _, ln in lines if not LEADER.search(ln))
    facts: dict = {}
    if notice or mentions:
        for i, (pg, ln) in enumerate(lines):
            s = _clean(ln)
            if not s or LEADER.search(s):
                continue
            nxt = _clean(lines[i + 1][1]) if i + 1 < len(lines) else ""

            def put(key, text):
                facts.setdefault(key, {"text": text[:220], "label": label, "page": pg})
            m = CONTRACT_NO.search(s)
            if m and "contract" not in facts:
                put("contract", f"Contract No. {m.group(1).rstrip('.')}")
            if "term" not in facts and TERM_RX.search(s):
                put("term", f"\"{s}\"")
            if "current" not in facts and CURRENT_RX.search(s) and (DATE_NEAR.search(s) or DATE_NEAR.search(nxt)):
                put("current", f"\"{s if DATE_NEAR.search(s) else s + ' ' + nxt}\"")
            if "pricing" not in facts and PRICING_RX.search(s):
                put("pricing", f"\"{s}\"")
            if "changes" not in facts and CHANGES_RX.search(s):
                put("changes", f"\"{s}\"")
    return {"notice": notice, "mentions": mentions, "facts": facts,
            "_renewal_doc": renewal_doc, "_new_doc": new_doc}


def merge_renewal(acc: dict, one: dict) -> dict:
    acc["_renewal_doc"] = acc["_renewal_doc"] or bool(one.get("_renewal_doc", one.get("notice")))
    acc["_new_doc"] = acc["_new_doc"] or bool(one.get("_new_doc"))
    acc["notice"] = acc["_renewal_doc"] and not acc["_new_doc"]
    acc["mentions"] = acc["mentions"] or bool(one.get("mentions"))
    for k, e in one.get("facts", {}).items():
        acc["facts"].setdefault(k, e)
    return acc


def renewal_block(ren: dict) -> str:
    """The block stored at the top of Scope_of_Work for a renewal notice ('' when the packet is not one)."""
    if not ren.get("notice"):
        return ""
    out = [f"{RENEWAL_HEAD} (read by rule from the notice; check against the document)"]
    for key, label in RENEWAL_FACTS:
        e = ren["facts"].get(key)
        if e:
            out.append(f"• {label}: {e['text']} ({_cite(e)})")
    if len(out) == 1:
        out.append("• No contract number, renewal term, pricing or scope language found by rule. Read the notice.")
    return "\n".join(out)


def split_renewal_block(text) -> tuple:
    """(block lines, text without the block). The block is RENEWAL_HEAD plus the bullet lines under it."""
    lines = str(text or "").splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith(RENEWAL_HEAD):
            j = i + 1
            while j < len(lines) and lines[j].lstrip().startswith("•"):
                j += 1
            rest = lines[:i] + lines[j:]
            return lines[i:j], "\n".join(rest).strip("\n")
    return [], str(text or "")

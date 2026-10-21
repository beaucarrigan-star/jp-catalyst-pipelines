# terms_extractor.py -- Phase 3c. Rule-based extraction of contract term fields.
#
# Keyword-anchored sentence windows (term, renew, notice, terminate, payment, invoice, insurance,
# governing law, not-to-exceed, escalator) with number/duration/date parsing. Each finding carries the
# sentence it came from, a page citation and a confidence:
#   High   -- explicit statement with digits (or 'thirty (30)'), one consistent value in the packet
#   Medium -- derived (end date = start + initial term; file-name expiry date) or several values where
#             the majority wins
#   Low    -- weak cue only; never written to the Contract, listed in the Note for the reviewer
#
# Output keys are Contracts API names. Picklist values are only proposed when they exist in the live
# picklist (ALLOWED below, read from Zoho on 28 Sep 2026); anything else goes to the Note so a person
# can add the picklist value in Setup -- writing a value that isn't in the list would be rejected.

import re
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta

from text_norm import (NUM_RE, add_months, filename_date, find_dates, join_pages, parse_money,
                       parse_num, sentences, source_ref)

ALLOWED = {
    "Auto_Renews": {"Yes", "No", "Not stated"},
    "Invoice_Cadence": {"Monthly", "Quarterly", "Annual", "Other"},
    "Governing_State": {"WI", "IA", "ID", "MI", "MN", "MO", "OH", "VA", "WV", "WY", "N/A"},
}

STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}
def _n(p):
    return p.replace("<NUM>", NUM_RE)


_STATE_RE = "|".join(sorted(STATES, key=len, reverse=True))


@dataclass
class Finding:
    field: str
    value: object
    confidence: str
    evidence: str
    source: str
    rule: str

    def as_note(self):
        v = self.value
        return "{} = {} ({}, {}; {})".format(self.field, v, self.confidence, self.source, self.rule)


# ---------------------------------------------------------------------------------------------
# Duration helpers
# ---------------------------------------------------------------------------------------------
_DUR = re.compile(_n(r"(?P<n><NUM>)\s*[- ]?\s*(?P<u>year|yr|month|mo|day)s?\b"), re.I)


def _months(n, unit):
    unit = unit.lower()
    if unit.startswith("y"):
        return n * 12
    if unit.startswith("mo") or unit.startswith("month"):
        return n
    return None


# ---------------------------------------------------------------------------------------------
# Individual rules. Each returns a list of (value, confidence, evidence_sentence, rule) candidates.
# ---------------------------------------------------------------------------------------------

_TERM_CUE = re.compile(r"\b(?:term|commence|begin|effective|through|until|expire|ending|period of performance)\b", re.I)
_RANGE = re.compile(
    r"(?:from|beginning|commencing|starting|effective)?\s*(?:on\s+)?(?P<a>{d})\s*(?:,\s*)?"
    r"(?:through|thru|to|until|and\s+(?:ending|expiring|terminating)\s+(?:on\s+)?|-)\s*(?:and\s+including\s+)?(?P<b>{d})".format(
        d=r"[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+20\d{2}|\d{1,2}/\d{1,2}/(?:20)?\d{2}|20\d{2}-\d{2}-\d{2}"),
    re.I)


def _term_dates(sents):
    """(start, end) candidates. A date range inside a sentence with a term cue is High."""
    starts, ends = [], []
    for s in sents:
        if not _TERM_CUE.search(s):
            continue
        for m in _RANGE.finditer(s):
            da, db = find_dates(m.group("a")), find_dates(m.group("b"))
            if da and db and db[0][0] > da[0][0]:
                starts.append((da[0][0], "High", s, "term date range"))
                ends.append((db[0][0], "High", s, "term date range"))
        if re.search(r"\b(?:expire|terminate|end)s?\s+(?:on|at\s+midnight\s+on)\b", s, re.I) and not _RANGE.search(s):
            ds = find_dates(s)
            if ds and re.search(r"term|agreement|contract", s, re.I):
                ends.append((ds[-1][0], "High", s, "expires on"))
        if re.search(r"\bcommenc\w*\s+on\b|\bbegin\w*\s+on\b", s, re.I) and not _RANGE.search(s):
            ds = find_dates(s)
            if ds:
                starts.append((ds[0][0], "High", s, "commences on"))
    return starts, ends


_INITIAL = re.compile(_n(
    r"(?:initial\s+term|term\s+of\s+this\s+(?:agreement|contract)|(?:agreement|contract)\s+shall\s+(?:be\s+in\s+effect|continue|remain\s+in\s+effect))"
    r"\s+(?:shall\s+be|is|of|will\s+be)?\s*(?:for\s+)?(?:a\s+(?:period|term)\s+of\s+)?(?P<n><NUM>)\s*[- ]?\s*(?P<u>year|month)s?\b"), re.I)


def _initial_term_months(sents):
    """'The initial term of this Agreement is three (3) years' -> 36. Only a duration that directly
    follows the term phrase counts ('after the first twelve (12) months of the Initial Term' does not)."""
    for s in sents:
        m = _INITIAL.search(s)
        if m:
            n = parse_num(m.group("n"))
            mo = _months(n, m.group("u")) if n else None
            if mo:
                return mo, s
    return None, None


_AUTO_YES = re.compile(r"automatic(?:ally)?\s+(?:be\s+)?(?:renew|extend)|shall\s+renew\s+automatically|auto[- ]?renew|evergreen", re.I)
_AUTO_NO = re.compile(r"(?:shall|will)\s+not\s+(?:be\s+)?(?:automatically\s+)?renew|no\s+automatic\s+renewal|not\s+(?:be\s+)?subject\s+to\s+automatic", re.I)
_RENEW_OPTIONAL = re.compile(r"(?:may|option\s+to)\s+(?:be\s+)?(?:renew|extend)|renew\w*\s+(?:by|upon)\s+mutual|mutual(?:ly)?\s+(?:written\s+)?(?:agree\w*\s+)?(?:to\s+)?(?:renew|extend)", re.I)
_OPTIONS = re.compile(_n(
    r"(?:up\s+to\s+)?(?P<n><NUM>)\s+(?:additional|successive|further|renewal)\s+(?:(?P<len><NUM>)\s*[- ]?\s*\(?\d*\)?\s*[- ]?(?P<u>year|month)s?\s*)?"
    r"(?:renewal\s+)?(?:terms?|periods?|options?|extensions?)"
    r"(?:\s+of\s+(?P<len2><NUM>)\s*[- ]?(?P<u2>year|month)s?)?"), re.I)
# "the option for two, 1-year extensions" (JusticePoint template, Monroe OH)
_OPTIONS2 = re.compile(_n(
    r"(?P<n><NUM>)\s*,?\s+(?P<len>\d+|<NUM>)\s*[- ]\s*(?P<u>year|month)\s+(?:renewal\s+)?(?:extensions?|renewals?|renewal\s+terms?|options?)"), re.I)
_RENEWAL_LEN = re.compile(_n(r"(?:renew|extend)\w*[^.]{0,60}?(?:for\s+)?(?:additional|successive|further)?\s*(?P<n><NUM>)\s*[- ]?\s*(?:\(\d+\)\s*)?[- ]?(?P<u>year|month)s?\b"), re.I)


def _renewal(sents):
    out = {}
    for s in sents:
        if not re.search(r"renew|extend|extension", s, re.I):
            continue
        if "Auto_Renews" not in out:
            if _AUTO_NO.search(s):
                out["Auto_Renews"] = ("No", "High", s, "states no automatic renewal")
            elif _AUTO_YES.search(s):
                out["Auto_Renews"] = ("Yes", "High", s, "automatically renews")
            elif _RENEW_OPTIONAL.search(s):
                out["Auto_Renews"] = ("No", "Medium", s, "renewal is optional/by agreement")
        m2o = _OPTIONS2.search(s)
        if m2o and "Renewal_Options_Left" not in out:
            n = parse_num(m2o.group("n"))
            mo = _months(parse_num(m2o.group("len")) or 0, m2o.group("u"))
            if n and n <= 10 and mo:
                out["Renewal_Options_Left"] = (n, "High", s, "N, M-year extensions")
                out.setdefault("Renewal_Option_Length_Mo", (mo, "High", s, "length of each extension"))
        m = _OPTIONS.search(s)
        if m and "Renewal_Options_Left" not in out:
            n = parse_num(m.group("n"))
            if n and n <= 10:
                out["Renewal_Options_Left"] = (n, "High", s, "N additional terms")
            ln, u = m.group("len") or m.group("len2"), m.group("u") or m.group("u2")
            if ln and u:
                mo = _months(parse_num(ln) or 0, u)
                if mo:
                    out["Renewal_Option_Length_Mo"] = (mo, "High", s, "length of each renewal")
        if "Renewal_Option_Length_Mo" not in out:
            m2 = _RENEWAL_LEN.search(s)
            if m2 and re.match(r"\s*(?:\(\d+\)\s*)?(?:prior\s+)?(?:written\s+)?notice|\s*(?:in\s+advance|before|prior)", s[m2.end():], re.I):
                m2 = None  # "not less than 3 months written notice" is a notice period, not a renewal length
            if m2:
                n = parse_num(m2.group("n"))
                mo = _months(n, m2.group("u")) if n else None
                if mo:
                    out["Renewal_Option_Length_Mo"] = (mo, "High", s, "renews for N-year terms")
    return out


_NOTICE_DAYS = re.compile(_n(r"(?P<n><NUM>)\s*(?:calendar\s+|business\s+)?days?'?\s*(?:'s)?\s*(?:prior\s+|advance\s+)?(?:written\s+)?(?:notice|prior|before|in\s+advance)"), re.I)
_NOTICE_DAYS2 = re.compile(_n(r"(?:notice|notify)[^.]{0,60}?(?:at\s+least\s+|no\s+(?:less|fewer)\s+than\s+)?(?P<n><NUM>)\s*(?:calendar\s+)?days?"), re.I)
_NOTICE_MONTHS = re.compile(_n(r"(?:at\s+least\s+|no\s+(?:less|fewer)\s+than\s+|not\s+less\s+than\s+)?(?P<n><NUM>)\s*months?'?\s*(?:prior\s+|advance\s+)?(?:written\s+)?notice"), re.I)
# "may be terminated by either party ... by giving written notice ... ninety (90) days in advance" with no
# breach/cause condition is termination for convenience in all but name (Monroe OH, JusticePoint template)
_EITHER_PARTY_TERM = re.compile(r"terminat\w*\s+by\s+either\s+party|either\s+party\s+(?:may|can|shall\s+have\s+the\s+right\s+to)\s+terminate", re.I)
_FOR_CAUSE = re.compile(r"breach|default|for\s+cause|material|fail|insolv|bankrupt|non-?appropriat", re.I)
_CONVENIENCE = re.compile(r"convenience|without\s+cause|for\s+any\s+reason|for\s+no\s+reason|at\s+will", re.I)


def _notice(sents):
    notice, tfc = [], []
    for s in sents:
        ms = list(_NOTICE_DAYS.finditer(s)) or list(_NOTICE_DAYS2.finditer(s))
        mult = 1
        if not ms:
            ms = list(_NOTICE_MONTHS.finditer(s))
            mult = 30  # "3 months written notice" -> 90 days
        if not ms:
            continue
        n = parse_num(ms[0].group("n"))
        n = n * mult if n else n
        if not n or n > 365:
            continue
        if _CONVENIENCE.search(s) or (_EITHER_PARTY_TERM.search(s) and not _FOR_CAUSE.search(s)):
            tfc.append((n, "High", s, "termination for convenience notice"))
        elif re.search(r"renew|end\s+(?:of|to)\s+the\s+(?:initial\s+)?term|expiration|non-?renewal", s, re.I) \
                and not re.search(r"insurance|polic(?:y|ies)|certificate", s, re.I):
            notice.append((n, "High", s, "notice before renewal/term end"))
    return notice, tfc


_PAY = re.compile(_n(r"(?:within|in)\s+(?P<n><NUM>)\s*(?:calendar\s+)?days?\s+(?:of|from|after|following)\s+(?:the\s+)?(?:date\s+of\s+)?(?:receipt\s+of\s+)?(?:an?\s+|the\s+|such\s+|each\s+)?(?:proper\s+|correct\s+|valid\s+)?invoice|net\s*[- ]?(?P<net>\d{2})\b"), re.I)


def _payment(sents):
    vals = []
    for s in sents:
        for m in _PAY.finditer(s):
            n = parse_num(m.group("n")) if m.group("n") else int(m.group("net"))
            if n and n <= 120:
                vals.append((n, s))
    if not vals:
        return None
    c = Counter(v for v, _ in vals)
    best, cnt = c.most_common(1)[0]
    conf = "High" if len(c) == 1 else "Medium"
    ev = next(s for v, s in vals if v == best)
    rule = "invoice payment days" if len(c) == 1 else "most common of {}".format(dict(c))
    return best, conf, ev, rule


_CADENCE = [
    ("Monthly", re.compile(r"invoice\w*\s+(?:\w+\s+){0,4}monthly|monthly\s+(?:invoice|billing|statement)|"
                           r"(?:billed|invoiced)\s+(?:on\s+a\s+)?monthly|monthly\s+basis|each\s+month|per\s+calendar\s+month", re.I)),
    ("Quarterly", re.compile(r"invoice\w*\s+(?:\w+\s+){0,4}quarterly|quarterly\s+(?:invoice|billing)|(?:billed|invoiced)\s+(?:on\s+a\s+)?quarterly", re.I)),
    ("Annual", re.compile(r"invoice\w*\s+(?:\w+\s+){0,4}annually|annual\s+(?:invoice|billing)|(?:billed|invoiced)\s+(?:on\s+an\s+)?annual", re.I)),
]


def _cadence(sents):
    votes = Counter()
    ev = {}
    for s in sents:
        if not re.search(r"invoice|bill", s, re.I):
            continue
        for val, rx in _CADENCE:
            if rx.search(s):
                votes[val] += 1
                ev.setdefault(val, s)
    if not votes:
        return None
    best, cnt = votes.most_common(1)[0]
    return best, ("High" if len(votes) == 1 else "Medium"), ev[best], "invoice cadence ({} mention(s))".format(cnt)


_GOV = re.compile(r"(?:governed\s+by|construed\s+(?:in\s+accordance\s+with|under)|laws\s+of)[^.]{0,80}?(?:state\s+of\s+|commonwealth\s+of\s+)?(?P<st>%s)\b" % _STATE_RE, re.I)


def _governing(sents):
    for s in sents:
        if re.search(r"govern|construed|applicable\s+law|choice\s+of\s+law", s, re.I):
            m = _GOV.search(s)
            if m:
                return STATES[m.group("st").lower()], "High", s, "governing law clause"
    return None


_NTE = re.compile(r"(?:not\s+to\s+exceed|shall\s+not\s+exceed|maximum\s+(?:contract\s+)?(?:amount|value|compensation)\s+(?:of|is|shall\s+be)|total\s+(?:contract\s+)?(?:amount|value)\s+(?:of|shall\s+not\s+exceed))\s*(?:the\s+(?:sum|amount)\s+of\s+)?(?P<amt>\$[\d,]+(?:\.\d{2})?)", re.I)


def _nte(sents):
    for s in sents:
        m = _NTE.search(s)
        if m and not re.search(r"per\s+(?:day|unit|month|participant|client)", s[m.end():m.end() + 30], re.I):
            v = parse_money(m.group("amt"))
            if v and v >= 1000:
                return v, "High", s, "not-to-exceed amount"
    return None


_ESC = re.compile(r"(?:increase|escalat\w*|adjust\w*)[^.]{0,100}?(?P<pct>\d+(?:\.\d+)?)\s*%|(?P<pct2>\d+(?:\.\d+)?)\s*%\s*(?:annual|yearly)?\s*(?:increase|escalat\w*)", re.I)


def _escalator(sents):
    cpi = None
    for s in sents:
        if not re.search(r"increase|escalat|adjust", s, re.I):
            continue
        if re.search(r"price|pricing|rate|fee", s, re.I) and re.search(r"annual|each\s+year|per\s+year|yearly|anniversary", s, re.I):
            m = _ESC.search(s)
            if m and not re.search(r"interest|late|past\s+due|per\s+month", s, re.I):
                return ("pct", float(m.group("pct") or m.group("pct2")), "High", s, "annual escalator %")
            if re.search(r"consumer\s+price\s+index|\bCPI\b", s, re.I):
                cpi = ("cpi", None, "Medium", s, "CPI-based escalator (no fixed %)")
    return cpi


_INS = re.compile(r"(?:general\s+liability|liability\s+insurance)[^.]{0,120}?(?P<amt>\$[\d,]{5,}(?:\.\d{2})?)\s*(?:per\s+occurrence|each\s+occurrence|combined)?", re.I)


def _insurance(sents):
    for s in sents:
        m = _INS.search(s)
        if m:
            return parse_money(m.group("amt")), "High", s, "general liability per-occurrence limit"
    for s in sents:
        if re.search(r"insurance", s, re.I):
            m = re.search(r"(?P<amt>\$[\d,]{7,}(?:\.\d{2})?)\s*(?:per|each)\s+occurrence", s, re.I)
            if m:
                return parse_money(m.group("amt")), "Medium", s, "first per-occurrence insurance amount"
    return None


# ---------------------------------------------------------------------------------------------
# Document + packet level
# ---------------------------------------------------------------------------------------------

def _cite(packet_name, file_name, joined, offsets, evidence):
    pos = joined.find(evidence[:60])
    page = 1
    for i, off in enumerate(offsets):
        if pos >= off:
            page = i + 1
    return source_ref(packet_name, file_name, page)


def extract_terms(file_name, page_texts, doc_type, effective_date, packet_name="packet.zip"):
    """Findings from ONE document -> list[Finding]. doc_type drives what a document may speak to:
    amendments/addenda can change end date, renewal and pricing terms but their 'effective' date is the
    amendment's own date, never the contract start."""
    joined, offsets = join_pages(page_texts)
    sents = sentences(joined.replace("\f", " "))
    out = []

    def add(fld, val, conf, ev, rule):
        out.append(Finding(fld, val, conf, ev[:400], _cite(packet_name, file_name, joined, offsets, ev), rule))

    starts, ends = _term_dates(sents)
    if doc_type != "Base agreement":
        starts = []  # an amendment's own dates are not the contract's start
    if starts:
        d, conf, ev, rule = starts[0]
        add("Contract_Start_Date", d.isoformat(), conf, ev, rule)
    if ends:
        d, conf, ev, rule = max(ends, key=lambda x: x[0])
        add("Contract_End_Date", d.isoformat(), conf, ev, rule)

    months, ev = _initial_term_months(sents)
    start_val = starts[0][0] if starts else None
    if not start_val and doc_type == "Base agreement" and effective_date:
        from datetime import date as _d
        start_val = _d.fromisoformat(effective_date)
        add("Contract_Start_Date", effective_date, "Medium", "document effective date " + effective_date,
            "agreement effective date")
    if months and start_val and not ends:
        end = add_months(start_val, months) - timedelta(days=1)
        add("Contract_End_Date", end.isoformat(), "Medium", ev, "start + {}-month initial term".format(months))
    if not ends and not months and doc_type == "Base agreement":
        m = re.search(r"expir\w*[\s_-]*(20\d{6}|20\d{2}[-_]\d{2}[-_]\d{2})", file_name, re.I)
        fd = filename_date(m.group(1)) if m else None
        if fd:
            add("Contract_End_Date", fd.isoformat(), "Medium", "file name: " + file_name, "expiry date in file name")

    # Original_End_Date: the end of the first term as signed. Only a base agreement can state it; an
    # amendment's later end date moves Contract_End_Date but never this.
    if doc_type == "Base agreement":
        ce = next((f for f in out if f.field == "Contract_End_Date"), None)
        if ce:
            out.append(Finding("Original_End_Date", ce.value, ce.confidence, ce.evidence, ce.source, ce.rule))

    for fld, (val, conf, ev, rule) in _renewal(sents).items():
        add(fld, val, conf, ev, rule)
        if fld == "Renewal_Options_Left":
            # "N additional terms" in the paper is the total the contract allows. Renewals_Allowed keeps that
            # total; Renewal_Options_Left is what a person adjusts when some have been used.
            add("Renewals_Allowed", val, conf, ev, rule)

    notice, tfc = _notice(sents)
    if notice:
        add("Notice_Period_Days", *notice[0])
    if tfc:
        add("Termination_Convenience_Days", *tfc[0])

    for fld, fn in (("Payment_Terms_Days", _payment), ("Invoice_Cadence", _cadence),
                    ("Governing_State", _governing), ("Not_To_Exceed", _nte), ("Insurance_Limit", _insurance)):
        r = fn(sents)
        if r:
            add(fld, *r)

    esc = _escalator(sents)
    if esc:
        kind, val, conf, ev, rule = esc
        if kind == "pct":
            add("Escalator_Pct", val, conf, ev, rule)
        else:
            add("Escalator_Note", "CPI", conf, ev, rule)  # not a field: reported in the Note
    return out


def resolve_terms(doc_findings):
    """doc_findings: list of (effective_date, doc_type, [Finding]). Later document wins per field, except
    Contract_Start_Date which comes from the base agreement. Returns {field: Finding}."""
    ordered = sorted(doc_findings, key=lambda x: (x[0] or ""))
    best = {}
    for eff, dtype, finds in ordered:
        for f in finds:
            cur = best.get(f.field)
            if f.field in ("Contract_Start_Date", "Original_End_Date") and cur and dtype != "Base agreement":
                continue
            # within the same document keep the higher confidence
            if cur and cur.source.split(",")[0] == f.source.split(",")[0] and _rank(cur) >= _rank(f):
                continue
            best[f.field] = f
    return best


def _rank(f):
    return {"High": 3, "Medium": 2, "Low": 1}[f.confidence]


def allowed_value(fld, value):
    """True when a picklist value can be written as-is."""
    if fld in ALLOWED:
        return value in ALLOWED[fld]
    return True

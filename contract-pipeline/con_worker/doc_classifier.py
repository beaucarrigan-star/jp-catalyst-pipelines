# doc_classifier.py
# Rule-based classification only -- no AI tokens at runtime (Phase 0 decision). Picks Doc_Type and a
# best-guess Effective_Date so Contract_Documents can be indexed and ordered correctly.
# 28 Sep 2026 (Phase 3b/3c session): guess_effective_date now delegates to
# text_norm.document_effective_date (handles "entered on February 1, 2026", "as of the date last
# signed", "Effective Date: ...", and ignores the parent "Agreement Dated January 1, 2016"), and
# extract_applies_to_contracts reads Allied's contract-number table, which the old "applies to: 592, 835"
# regex missed.

import re

from text_norm import document_effective_date, normalise

_TYPE_RULES = [
    ("Addendum", re.compile(r"\baddendum\b|\baddend\b", re.I)),
    ("Amendment", re.compile(r"\bamendment\b|\bamend\b", re.I)),
    ("Rate sheet", re.compile(r"\brate\s*sheet\b|\bpricing\b|\brate\s*card\b|\btariff\b|price\s+list|"
                              r"product\s+and\s+service\s+schedule", re.I)),
    ("Base agreement", re.compile(r"\bagreement\b|\bcontract\b|\bmsa\b", re.I)),
]


def guess_effective_date(file_name, text):
    """ISO date or None. See text_norm.document_effective_date for the rule order."""
    iso, _how = document_effective_date(file_name, text)
    return iso


def classify(file_name, text_head):
    """File name decides first (it is what people type deliberately: 'Addendum 24 - ...'); the text head
    only when the name says nothing."""
    name = re.sub(r"[_\-.]+", " ", file_name.rsplit("/", 1)[-1])  # "SP Agreement_Expires" -> word boundaries
    for doc_type, pattern in _TYPE_RULES:
        if pattern.search(name):
            return doc_type
    head = (text_head or "")[:2000]
    for doc_type, pattern in _TYPE_RULES:
        if pattern.search(head):
            return doc_type
    return "Other"


def extract_applies_to_contracts(text):
    """Allied-style addenda list the contract numbers they apply to, either inline ('applies to: 592,
    835') or as a table ('applies only to the following Channel Partner contracts ... 592 Justice Point
    835 Justice Point - Enhanced Monitoring ...'). Returns '592,835,...' or ''."""
    t = normalise(text or "")
    m = re.search(r"appl(?:y|ies)\s+(?:only\s+)?to\s+(?:the\s+following\s+)?[^.:]{0,80}?(?:contracts?|accounts?)?\s*[:\-]?", t, re.I)
    if not m:
        return ""
    window = t[m.end():m.end() + 700]
    stop = re.search(r"\[|\bThe\s+Parties\b|\b2\.\s+[A-Z]|IN\s+WITNESS", window)
    if stop:
        window = window[:stop.start()]
    nums = re.findall(r"(?<![\d$.,])(\d{3,5})(?=\s*(?:,|/|and\b|[A-Z]))", window)
    seen = []
    for n in nums:
        if n not in seen:
            seen.append(n)
    return ",".join(seen)

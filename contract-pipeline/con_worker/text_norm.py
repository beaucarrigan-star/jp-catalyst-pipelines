# text_norm.py -- shared text helpers for the Phase 3 extractors (rates 3b, terms 3c).
#
# Everything here is rule-based (no AI tokens at runtime, Phase 0 decision). Two jobs:
#   1. Normalise extracted text so the same regexes work on pdfplumber output (line per row) and on
#      flattened text (whole page on one line, which is what Graph/SharePoint previews and some
#      DOCX exports produce). Newlines are NOT relied on anywhere downstream.
#   2. Parse the small vocabulary contracts use for amounts, numbers, durations and dates.

import re
from datetime import date, datetime

# ---------------------------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------------------------

_MONEY_SPLIT = re.compile(r"\$\s*(\d[\d,]*)\s*\.\s*(\d)\s*(\d)(?!\d)")
_DOLLAR_SPACE = re.compile(r"\$\s+(?=\d)")


def normalise(text):
    """Collapse whitespace, straighten quotes/dashes, and re-join money amounts that PDF text
    extraction split apart ("$4. 17" -> "$4.17", "$ 8 .5 3" -> "$8.53", "$0. 3 5" -> "$0.35").
    Only splits AFTER a decimal point are joined; a space inside the integer part ("$1 500") is left
    alone because it can't be told apart from two adjacent table cells ("$54 1.00")."""
    if not text:
        return ""
    t = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = t.replace("–", "-").replace("—", "-").replace(" ", " ")
    t = re.sub(r"\s+", " ", t)
    t = _MONEY_SPLIT.sub(lambda m: "${}.{}{}".format(m.group(1), m.group(2), m.group(3)), t)
    t = _DOLLAR_SPACE.sub("$", t)
    return t.strip()


def split_pages(text):
    """packet_reader joins PDF pages with a form feed (\\f). Returns a list of page strings
    (1 element for DOCX/XLSX/TXT)."""
    if not text:
        return [""]
    return text.split("\f")


def sentences(text):
    """Very plain sentence splitter over normalised text. Keeps numbered-clause punctuation such as
    '9.1 Term.' from splitting inside the number."""
    parts = re.split(r"(?<=[a-z0-9\)\]])[.;]\s+(?=[A-Z0-9\(])", text)
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------------------------------------
# Numbers and money
# ---------------------------------------------------------------------------------------------

_WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
    "twenty-four": 24, "thirty": 30, "thirty-six": 36, "forty": 40, "forty-five": 45, "sixty": 60,
    "seventy-five": 75, "ninety": 90, "one hundred twenty": 120, "one hundred eighty": 180,
}
NUM_WORD_RE = "|".join(sorted((re.escape(k) for k in _WORD_NUMS), key=len, reverse=True))
# "thirty (30)", "30", "thirty"
NUM_RE = r"(?:[A-Za-z]+(?:-[A-Za-z]+)?\s*\(\s*\d+\s*\)|\d+|(?:{w}))".format(w=NUM_WORD_RE)


def parse_num(s):
    """'thirty (30)' -> 30, '30' -> 30, 'two' -> 2. The parenthetical digits win when both exist."""
    if s is None:
        return None
    s = s.strip().lower()
    m = re.search(r"\(\s*(\d+)\s*\)", s)
    if m:
        return int(m.group(1))
    m = re.fullmatch(r"\d+", s)
    if m:
        return int(s)
    return _WORD_NUMS.get(s)


MONEY_RE = re.compile(r"\$(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?")


def parse_money(s):
    m = MONEY_RE.search(s or "")
    if not m:
        return None
    whole = int(m.group(1).replace(",", ""))
    cents = int(m.group(2)) if m.group(2) else 0
    return round(whole + cents / 100.0, 2)


# ---------------------------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------------------------

_MONTHS = ("January|February|March|April|May|June|July|August|September|October|November|December|"
           "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec")
DATE_RE = re.compile(
    r"\b(?:(?P<mon>{m})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y>20\d{{2}})"
    r"|(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:day\s+of\s+)?(?P<mon2>{m}),?\s+(?P<y2>20\d{{2}})"
    r"|(?P<iy>20\d{{2}})-(?P<im>\d{{2}})-(?P<id>\d{{2}})"
    r"|(?P<um>\d{{1,2}})/(?P<ud>\d{{1,2}})/(?P<uy>20\d{{2}}|\d{{2}}))\b".format(m=_MONTHS),
    re.I,
)


def _mon(name):
    name = name[:3].title()
    return datetime.strptime(name, "%b").month


def date_from_match(m):
    try:
        if m.group("mon"):
            return date(int(m.group("y")), _mon(m.group("mon")), int(m.group("d")))
        if m.group("mon2"):
            return date(int(m.group("y2")), _mon(m.group("mon2")), int(m.group("d2")))
        if m.group("iy"):
            return date(int(m.group("iy")), int(m.group("im")), int(m.group("id")))
        if m.group("um"):
            y = int(m.group("uy"))
            if y < 100:
                y += 2000
            return date(y, int(m.group("um")), int(m.group("ud")))
    except ValueError:
        return None
    return None


def find_dates(text):
    """[(date, start, end)] for every parseable date in text."""
    out = []
    for m in DATE_RE.finditer(text or ""):
        d = date_from_match(m)
        if d:
            out.append((d, m.start(), m.end()))
    return out


def add_months(d, months):
    y = d.year + (d.month - 1 + months) // 12
    mth = (d.month - 1 + months) % 12 + 1
    # clamp day
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, mth, day)
        except ValueError:
            continue
    return None


def add_term_months(d, months):
    """Contract-term arithmetic: d + months, keeping a month-end date at month-end.
    2027-02-28 + 12 -> 2028-02-29 (28 Feb is the last day of February, so is 29 Feb 2028);
    2026-04-30 + 1 -> 2026-05-31; 2026-06-15 + 12 -> 2027-06-15. Other days clamp to the month's length.
    (add_months above is the older clamp-only helper that start + initial term uses; leave it alone.)"""
    import calendar
    idx = d.month - 1 + months
    y, m = d.year + idx // 12, idx % 12 + 1
    last = calendar.monthrange(y, m)[1]
    on_last = d.day == calendar.monthrange(d.year, d.month)[1]
    return date(y, m, last if on_last else min(d.day, last))


def filename_date(name):
    """Dates in file names: 2026-02-01, 2026_02_01, 20270109, 02-01-2026."""
    m = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", name or "")
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = re.search(r"(\d{2})[-_](\d{2})[-_](20\d{2})", name or "")
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    return None


_ENTERED_AS_OF = re.compile(
    r"(?:(?:is|was)\s+entered\s+(?:into\s+)?(?:on|as\s+of)|(?:is|shall\s+be)\s+effective\s+(?:as\s+of|on)|"
    r"entered\s+(?:into\s+)?(?:on|as\s+of))\s*(?P<rest>.{0,60})",
    re.I,
)
_LAST_SIGNED = re.compile(r"(?:date\s+last\s+signed|last\s+date\s+signed|date\s+last\s+signed\s+by)", re.I)


def document_effective_date(file_name, text):
    """Effective date of ONE document (base agreement, amendment, addendum), for later-document-wins.

    Order: (1) an explicit 'entered into as of <date>' / 'effective as of <date>' in the first ~1500
    chars that is not the parent agreement's date ('Agreement Dated January 1, 2016'); (2) 'as of the
    date last signed' -> the latest date appearing in the signature block (last 1200 chars);
    (3) 'Effective Date' label followed by a date anywhere; (4) a date in the file name. Returns
    (iso_date or None, how) where `how` names the rule for the Note."""
    t = normalise(text)
    head = t[:1500]
    for m in _ENTERED_AS_OF.finditer(head):
        rest = m.group("rest")
        if re.match(r"\s*the\s+(date|last)", rest, re.I):
            continue
        ds = find_dates(rest)
        if ds and ds[0][1] < 25:
            return ds[0][0].isoformat(), "stated effective date"
    if _LAST_SIGNED.search(t):
        tail_dates = [d for d, _, _ in find_dates(t[-1500:])]
        if tail_dates:
            return max(tail_dates).isoformat(), "date last signed"
    for m in re.finditer(r"(?:\(the\s+\"?\s*Effective\s+Date\s*\"?\s*\)|Effective\s+Date\s*:?)\s*(.{0,40})", t, re.I):
        ds = find_dates(m.group(1))
        if ds:
            return ds[0][0].isoformat(), "Effective Date label"
    fd = filename_date(file_name)
    if fd:
        return fd.isoformat(), "file name date"
    return None, "none found"


# ---------------------------------------------------------------------------------------------
# Source citations
# ---------------------------------------------------------------------------------------------

def page_of(pages_norm, needle_start_global, offsets):
    """Given the global char offset of a match in the joined normalised text, return the 1-based
    page number using `offsets` (start offset of each page in the joined text)."""
    page = 1
    for i, off in enumerate(offsets):
        if needle_start_global >= off:
            page = i + 1
    return page


def join_pages(page_texts):
    """Normalise each page and join with ' \\f ' so page numbers can be recovered for citations.
    Returns (joined, offsets)."""
    offsets, parts, pos = [], [], 0
    for p in page_texts:
        n = normalise(p)
        offsets.append(pos)
        parts.append(n)
        pos += len(n) + 3
    return " \f ".join(parts), offsets


def source_ref(packet_name, file_name, page):
    base = file_name.rsplit("/", 1)[-1]
    if page:
        return "{} > {}, p.{}".format(packet_name, base, page)
    return "{} > {}".format(packet_name, base)

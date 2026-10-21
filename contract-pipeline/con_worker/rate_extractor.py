# rate_extractor.py -- Phase 3b. Rule-based rate extraction from contract packets.
#
# Input: the packet's documents (already classified and dated by doc_classifier/text_norm).
# Output: RateProposal objects (one per device/tier/effective date) plus contract-level pricing facts
# (Rate_Basis, Admin_Rate_Pct, spare-inventory rule). Nothing here talks to Zoho; writeback.py turns
# proposals into Rate_Lines changes under the Phase 0 upsert/supersede rules.
#
# How rates are found (works on pdfplumber text and on flattened one-line-per-page text alike):
#   1. Find pricing TABLE HEADERS ("Lease Rate Per Unit/Per Day", "Service Fee per Unit, per Day",
#      "Monthly Rental", "Replacement Cost", "Purchase Price"...). The header fixes which money columns
#      the rows under it have and what each column means (per day / per month / one-time / replacement).
#   2. Walk the money cells after the header, group them into rows of that many columns; the text
#      between the previous row and this one is the row label (device name, quantity tier).
#   3. Map the label to a Rate_Device through the synonym dictionary; read the tier ("1 - 50", "401+").
#   4. Prose rates outside tables: "$x.xx per day/per unit" sentences (spare overage, one-offs).
#   5. Cost-plus ("cost plus 12%", "actual cost ... plus an administrative fee of 12%") and purchased-unit
#      language set Rate_Basis / Admin_Rate_Pct.
#   6. Rental schedules that give a monthly rental AND a daily service fee (SCRAM/AMS) produce an
#      all-in daily vendor cost = monthly / 30.4375 + daily service fee, with the arithmetic written into
#      Rate_Terms so a person can check it.
# Later-document-wins is applied by the caller over proposals from all documents (resolve_current()).

import re
from dataclasses import dataclass, field
from typing import List, Optional

from text_norm import MONEY_RE, join_pages, parse_money, source_ref

DAYS_PER_MONTH = 30.4375  # 365.25 / 12 -- reproduces the SCRAM catalog rates (6.27, 3.82) to the cent

# ---------------------------------------------------------------------------------------------
# Device synonym dictionary. Order matters: most specific first. Values are Rate_Lines.Rate_Device
# picklist values exactly (GPS, Wrist-worn GPS, 2-Piece GPS, Remote Breath, CAM, CAM Landline,
# Hybrid GPS + Alcohol, Victim / DV, Other).
# ---------------------------------------------------------------------------------------------
DEVICE_SYNONYMS = [
    ("Hybrid GPS + Alcohol", re.compile(r"hybrid|gps\s*\+\s*alcohol|alcohol\s*\+\s*gps", re.I)),
    ("Victim / DV", re.compile(r"victim|domestic\s+violence|\bDV\b|survivor", re.I)),
    ("CAM Landline", re.compile(r"(?:\bCAM\b|continuous\s+alcohol|SCRAM\s*(?:CAM|bracelet))[^$]{0,80}?land\s*line"
                                r"|land\s*line[^$]{0,40}?(?:\bCAM\b|continuous\s+alcohol)", re.I)),
    ("CAM", re.compile(r"\bCAM\b|continuous\s+alcohol|transdermal|SCRAM\s+(?:bracelet|CAM)", re.I)),
    ("Remote Breath", re.compile(r"remote\s*breath|\bRB\b|breath\s*(?:alcohol|test|device|pro)|soberlink|cellular\s+breath", re.I)),
    ("Wrist-worn GPS", re.compile(r"wrist", re.I)),
    ("2-Piece GPS", re.compile(r"two\s*-?\s*piece|2\s*-?\s*piece", re.I)),
    ("GPS", re.compile(r"\bGPS\b|one\s*-?\s*piece|1\s*-?\s*piece|tracking\s+device|\bAT1\b|location\s+monitor", re.I)),
    ("Other", re.compile(r"house\s+arrest|\bRF\b|radio\s+frequency|curfew|voice\s+verification|smartphone|app\b", re.I)),
]

# Components that are not a Rate_Device on their own but feed a kit total (SCRAM rental schedule).
COMPONENTS = [
    ("cam_bracelet", re.compile(r"CAM\s+Bracelet", re.I)),
    ("wireless_base", re.compile(r"Wireless\s+Base\s+Station(?:\s+Standalone)?", re.I)),
    ("landline_base", re.compile(r"Landline\s+Base\s+Station\s+Standalone|CAM/House\s+Arrest\s+Landline", re.I)),
]


def device_for(label):
    for device, rx in DEVICE_SYNONYMS:
        if rx.search(label):
            return device
    return None


def component_for(label):
    # A "CAM Kit Landline Base Station" row is a full kit, not a component.
    if re.search(r"\bkit\b", label, re.I):
        return None
    for comp, rx in COMPONENTS:
        if rx.search(label):
            return comp
    return None


# ---------------------------------------------------------------------------------------------
# Table headers -> column kinds
# ---------------------------------------------------------------------------------------------
COL_PATTERNS = [
    ("month", re.compile(r"monthly\s+(?:rental|lease|fee|rate)|per\s+month|/\s*month", re.I)),
    ("day", re.compile(r"(?:lease|rental|service|monitoring)\s+(?:rate|fee)s?\s*(?:per\s*unit\s*[,/]?\s*)?per\s*day"
                       r"|per\s*unit\s*[,/]?\s*per\s*day|(?:daily|per\s+diem)\s+(?:rate|fee)|rate\s+per\s+day"
                       r"|lease\s+(?:rate|fee)|/\s*day\b", re.I)),
    ("one_time", re.compile(r"price\s+per\s+unit|purchase\s+price|one[\s-]*time|unit\s+price", re.I)),
    ("replacement", re.compile(r"replacement\s+(?:cost|fee|price)", re.I)),
]
# A header needs at least one of these words to count as a pricing table header.
HEADER_ANCHOR = re.compile(r"product\s+description|equipment\s+type|device\s+type|description|pricing|"
                           r"lost,?\s*damaged|rate\s+frequency", re.I)
END_OF_TABLE = re.compile(r"\[\s*(?:the\s+)?remainder|\bIN\s+WITNESS\b|\bNote\s+1\b|^\s*\d+\.\s+[A-Z]", re.I)


@dataclass
class Header:
    start: int
    end: int
    cols: List[str]


def _find_headers(t):
    """Return pricing-table headers in document order.

    A header is a run of column phrases each within 60 chars of the previous one (adjacent phrases of
    the same kind within 25 chars are one column: 'Lease Rate Per Unit/Per Day' is one 'day' column).
    It must sit near a header anchor ('Product Description', 'Equipment Type', ...), contain no money
    itself, and be followed by a money cell before the next header -- prose that merely mentions
    'the Lease Rate Per Unit/Per Day' is dropped by that last rule."""
    hits = []
    for kind, rx in COL_PATTERNS:
        for m in rx.finditer(t):
            hits.append([m.start(), m.end(), kind])
    hits.sort()
    merged = []
    for h in hits:
        if merged and h[0] < merged[-1][1]:  # overlap: keep the longer
            if (h[1] - h[0]) > (merged[-1][1] - merged[-1][0]):
                merged[-1] = h
            continue
        if merged and h[2] == merged[-1][2] and h[0] - merged[-1][1] <= 25:
            merged[-1][1] = h[1]
            continue
        merged.append(h)
    groups, group = [], []
    for h in merged:
        # a money cell between two phrases means the first header's rows have started (Monroe OH:
        # "Amount/Device/Day" + "$4.50" + "replacement cost" is two tables, not one header)
        if group and (h[0] - group[-1][1] > 60 or MONEY_RE.search(t[group[-1][1]:h[0]])):
            groups.append(group)
            group = []
        group.append(h)
    if group:
        groups.append(group)
    cand = []
    for g in groups:
        start, end = g[0][0], g[-1][1]
        if not HEADER_ANCHOR.search(t[max(0, start - 160):end]):
            continue
        if MONEY_RE.search(t[start:end]):
            continue
        cand.append(Header(start, end, [k for _, _, k in g]))
    out = []
    for i, h in enumerate(cand):
        nxt = cand[i + 1].start if i + 1 < len(cand) else len(t)
        first_cell = MONEY_RE.search(t, h.end)
        if not first_cell or first_cell.start() > nxt or first_cell.start() - h.end > 400:
            continue
        out.append(h)
    return out


@dataclass
class Cell:
    start: int
    end: int
    value: Optional[float]  # None = NA cell


def _cells(t, start, stop, multi_col):
    """Money cells in t[start:stop]. In multi-column tables 'NA'/'N/A' and bare decimals ('1.00')
    also count as cells, because vendor schedules leave the $ off or print NA."""
    rx = r"\$(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{2})?"
    if multi_col:
        rx += r"|(?<![\w.$])(?:N/?A)(?![\w/])|(?<![\w.$,])\d+\.\d{2}(?![\d%])"
    out = []
    for m in re.finditer(rx, t[start:stop]):
        s = m.group(0)
        v = None if s.upper().replace("/", "") == "NA" else parse_money(s if s.startswith("$") else "$" + s)
        out.append(Cell(start + m.start(), start + m.end(), v))
    return out


_TIER_RANGE = re.compile(r"(?<![\d.$])(\d{1,4})\s*(?:-|to|through)\s*(\d{1,4})(?![\d.%])")
_TIER_PLUS = re.compile(r"(?<![\d.$])(\d{1,4})\s*\+")


def _tier(label):
    """(min, max) from the last tier expression in the row label, or (None, None)."""
    last = None
    for m in _TIER_RANGE.finditer(label):
        last = (m.end(), int(m.group(1)), int(m.group(2)))
    for m in _TIER_PLUS.finditer(label):
        if not last or m.end() > last[0]:
            last = (m.end(), int(m.group(1)), None)
    if not last:
        return None, None
    return last[1], last[2]


@dataclass
class RateProposal:
    device: str
    amount: float
    unit: str                      # 'day' (the only unit written to Rate_Lines), 'month', 'one_time', 'replacement'
    tier_min: Optional[int]
    tier_max: Optional[int]
    effective_date: Optional[str]
    file_name: str
    page: int
    label: str
    confidence: str = "High"
    terms: str = ""                # becomes Rate_Terms
    source: str = ""               # becomes Source_Ref
    flags: List[str] = field(default_factory=list)
    ownership: str = ""            # "Purchased" for customer-owned-unit monitoring rates, else ""

    def key(self):
        return (self.device, self.tier_min or 0, self.effective_date, self.ownership)


@dataclass
class DocRates:
    lines: List[RateProposal]
    rate_basis: Optional[str] = None
    rate_basis_evidence: str = ""
    admin_rate_pct: Optional[float] = None
    spare_pct: Optional[int] = None
    spare_fee: Optional[float] = None
    spare_evidence: str = ""
    program: Optional[str] = None  # "Justice Point - Sheboygan County Sheriff" when pricing is program-specific
    notes: List[str] = field(default_factory=list)


def _page_for(offsets, pos):
    page = 1
    for i, off in enumerate(offsets):
        if pos >= off:
            page = i + 1
    return page


def _short(label, n=90):
    label = label.strip(" ,;:-")
    return label if len(label) <= n else "..." + label[-n:]


def extract_rates(file_name, page_texts, effective_date, packet_name="packet.zip", counterparty_type=None):
    """Rates from ONE document. page_texts: list of raw page strings. effective_date: ISO string or
    None (the document's own effective date; lines inherit it)."""
    t, offsets = join_pages(page_texts)
    res = DocRates(lines=[])

    headers = _find_headers(t)
    rental_rows = []  # (label, monthly, daily, page) for the kit arithmetic
    for i, h in enumerate(headers):
        next_start = headers[i + 1].start if i + 1 < len(headers) else len(t)
        money_cols = [c for c in h.cols]
        k = len(money_cols)
        # table ends at next header, an end-of-table marker, or a long gap without cells
        seg_end = next_start
        m_end = END_OF_TABLE.search(t, h.end)
        cells = _cells(t, h.end, seg_end, k >= 2)
        rows, cur, prev_end = [], [], h.end
        for c in cells:
            if cur == [] and (c.start - prev_end) > 260:
                break  # long prose gap: table is over
            if cur == [] and m_end and m_end.start() < c.start and m_end.start() > prev_end:
                break
            cur.append(c)
            if len(cur) == k:
                rows.append((t[prev_end:cur[0].start], cur))
                prev_end = cur[-1].end
                cur = []
        if cur:  # incomplete last row (e.g. a final bare number the regex missed) -> pad with None
            cur += [Cell(cur[-1].end, cur[-1].end, None)] * (k - len(cur))
            rows.append((t[prev_end:cur[0].start], cur))

        for label, rcells in rows:
            label_clean = label.replace("\f", " ")
            device = device_for(label_clean)
            comp = component_for(label_clean)
            tmin, tmax = _tier(label_clean)
            page = _page_for(offsets, rcells[0].start)
            vals = dict(zip(money_cols, [c.value for c in rcells]))
            if "month" in vals and "day" in vals:
                rental_rows.append((label_clean, vals.get("month"), vals.get("day"), page, device, comp))
                continue
            for kind, c in zip(money_cols, rcells):
                if c.value is None or kind != "day":
                    continue
                if not device:
                    res.notes.append("Unmatched rate row in {} p.{}: '{}' ${:.2f}/day -- no device match".format(
                        file_name, page, _short(label_clean, 60), c.value))
                    continue
                if comp and k >= 2:  # vendor schedule: a base station's own fee is a kit component
                    continue
                res.lines.append(RateProposal(
                    device=device, amount=c.value, unit="day", tier_min=tmin, tier_max=tmax,
                    effective_date=effective_date, file_name=file_name, page=page, label=_short(label_clean),
                ))

    _kit_lines(res, rental_rows, t, file_name, effective_date)
    _drop_component_fees(res)
    _tier_cleanup(res)
    _prose_rates(res, t, offsets, file_name, effective_date)
    _rate_basis(res, t, counterparty_type)
    _spares(res, t)
    _drop_one_time_charges(res)
    pm = _PROGRAM.search(t)
    if pm:
        res.program = pm.group("prog").strip(" .,:")

    for ln in res.lines:
        if _OWNED.search(ln.label):
            ln.ownership = "Purchased"

    for ln in res.lines:
        ln.source = source_ref(packet_name, file_name, ln.page)
        if not ln.terms:
            tier = ""
            if ln.tier_min is not None:
                tier = ", {}{} units".format(ln.tier_min, "-{}".format(ln.tier_max) if ln.tier_max else "+")
            ln.terms = "{}{}{} ({}{})".format(
                "Purchased units: " if ln.ownership else "", ln.device, tier, file_name.rsplit("/", 1)[-1].rsplit(".", 1)[0],
                ", eff. {}".format(ln.effective_date) if ln.effective_date else "")[:255]
    return res


_PROGRAM = re.compile(r"(?:pricing|equipment\s+and\s+pricing)\s+which\s+is\s+specific\s+to\s+the\s+program\s+"
                      r"(?:titled\s+|for\s+)?(?P<prog>[A-Z][^:.]{3,80}?)\s*[:.]", re.I)
_OWNED = re.compile(r"purchas|customer[- ]owned|owned\s+units|units\s+owned", re.I)


# 9 Oct 2026: replacement / lost-device / purchase prices ($800, $850, $1,100, $1,200) were being read as
# daily rates on Burnett, Waushara, Menominee and Price and then superseded the real daily rate. No EM
# device-day costs anywhere near this, so anything above it is a one-time charge and never a Rate Line.
MAX_DAILY_RATE = 100.0


def _drop_one_time_charges(res):
    keep = []
    for ln in res.lines:
        if ln.amount is not None and ln.amount > MAX_DAILY_RATE:
            res.notes.append("{} ${:,.2f} in {} p.{} is too large for a daily rate -- treated as a one-time charge "
                             "(replacement, lost device or purchase price), not written as a Rate Line".format(
                                 ln.device, ln.amount, ln.file_name, ln.page))
            continue
        keep.append(ln)
    res.lines = keep


def _drop_component_fees(res):
    """When a schedule gives both a purchased-equipment service fee and a rental all-in for the same
    device, the all-in (Medium, computed) is the vendor-cost line; the bare service fee is recorded in
    the all-in's Rate_Terms instead of becoming a second row with the same upsert key."""
    allin = {ln.device for ln in res.lines if ln.confidence == "Medium" and "rental" in ln.terms}
    keep = []
    for ln in res.lines:
        if ln.device in allin and not ln.terms:
            res.notes.append("{} service fee ${:.2f}/day (purchased equipment, {} p.{}) not written as its own "
                             "line -- the rental all-in line covers {}".format(
                                 ln.device, ln.amount, ln.file_name, ln.page, ln.device))
            continue
        keep.append(ln)
    res.lines = keep


def _tier_cleanup(res):
    """'1+' on a single-row device is 'no tier', not a tier starting at 1. A tier is kept when the
    device has several rows in this document, or the range has an explicit max, or starts above 1."""
    by_dev = {}
    for ln in res.lines:
        by_dev.setdefault(ln.device, []).append(ln)
    for dev, lns in by_dev.items():
        real_tiers = [ln for ln in lns if ln.tier_max is not None or (ln.tier_min or 0) > 1]
        if not real_tiers:
            for ln in lns:
                ln.tier_min, ln.tier_max = None, None


def _kit_lines(res, rental_rows, t, file_name, eff):
    """SCRAM-style schedule: monthly rental + daily service fee per component. All-in daily cost:
        kit row (e.g. 'CAM Kit Landline Base Station')  -> its own device line
        CAM (wireless) = CAM Bracelet + Wireless Base Station, per the schedule's 'kitted' note
    Confidence Medium: the arithmetic is ours, the inputs are the vendor's."""
    if not rental_rows:
        return
    comps = {}
    for label, monthly, daily, page, device, comp in rental_rows:
        if comp:
            comps[comp] = (monthly, daily, page, label)
            continue
        if device and monthly is not None and daily is not None:
            allin = round(monthly / DAYS_PER_MONTH + daily, 2)
            res.lines.append(RateProposal(
                device=device, amount=allin, unit="day", tier_min=None, tier_max=None, effective_date=eff,
                file_name=file_name, page=page, label=_short(label), confidence="Medium",
                terms="{}: rental ${:.2f}/mo / {} + service ${:.2f}/day = ${:.2f}/day".format(
                    _short(label, 40), monthly, DAYS_PER_MONTH, daily, allin)[:255],
            ))
    kitted = re.search(r"wireless\s+base\s+station\s+kitted\s+with\s+CAM", t, re.I)
    if kitted and "cam_bracelet" in comps and "wireless_base" in comps:
        (bm, bd, bp, _), (wm, wd, _, _) = comps["cam_bracelet"], comps["wireless_base"]
        if None not in (bm, bd, wm, wd):
            allin = round((bm + wm) / DAYS_PER_MONTH + bd + wd, 2)
            res.lines.append(RateProposal(
                device="CAM", amount=allin, unit="day", tier_min=None, tier_max=None, effective_date=eff,
                file_name=file_name, page=bp, label="CAM Bracelet + Wireless Base Station (kitted)",
                confidence="Medium",
                terms="CAM wireless kit: rental (${:.2f}+${:.2f})/mo / {} + service ${:.2f}+${:.2f}/day = ${:.2f}/day".format(
                    bm, wm, DAYS_PER_MONTH, bd, wd, allin)[:255],
            ))


_PROSE_DAILY = re.compile(
    r"(?P<pre>[^.]{0,160}?)\$(?P<amt>\d+\.\d{2})\s*(?:per|/|a)\s*"
    r"(?:(?:unit|participant|client|device|offender|defendant|person)\s*(?:per|/)\s*)?day(?:\s*(?:/|per)\s*(?:per\s+)?unit)?",
    re.I)


def _prose_rates(res, t, offsets, file_name, eff):
    """Daily rates written in sentences rather than tables. Spare-inventory overage fees are NOT rate
    lines (they go to the spare rule) and a sentence already covered by a table row is skipped."""
    have = {(ln.device, ln.amount) for ln in res.lines}
    for m in _PROSE_DAILY.finditer(t):
        pre = m.group("pre")
        if re.search(r"spare|overage|inventory|excess|above\s+\d+\s*%", pre, re.I):
            continue
        dev = device_for(pre)
        amt = float(m.group("amt"))
        if not dev or (dev, amt) in have:
            continue
        page = _page_for(offsets, m.start("amt"))
        tmin, tmax = _tier(pre)
        res.lines.append(RateProposal(
            device=dev, amount=amt, unit="day", tier_min=tmin, tier_max=tmax, effective_date=eff,
            file_name=file_name, page=page, label=_short(pre), confidence="Medium",
        ))
        have.add((dev, amt))


_COST_PLUS = [
    re.compile(r"cost\s*(?:\+|plus)\s*(?P<pct>\d+(?:\.\d+)?)\s*%", re.I),
    re.compile(r"(?:actual|vendor|manufacturer'?s?|equipment)\s+cost[^.]{0,120}?plus\s+(?:an?\s+)?"
               r"(?:administrative|admin|management|service)?\s*(?:fee|rate|mark-?up)?\s*(?:of\s+)?(?P<pct>\d+(?:\.\d+)?)\s*%", re.I),
    re.compile(r"(?P<pct>\d+(?:\.\d+)?)\s*%\s*(?:administrative|admin|management)\s+(?:fee|rate)[^.]{0,80}?(?:cost|invoice)", re.I),
]
_PURCHASED = re.compile(r"(?:purchase[sd]?\s+(?:units?|devices?|equipment)|units?\s+(?:owned|purchased)\s+by|"
                        r"one[\s-]*time\s+purchase\s*\(full\s+ownership\)|customer[- ]owned)", re.I)
_TIERED = re.compile(r"tiered\s+(?:quantity|pricing)|quantity\s+tiers", re.I)


def _rate_basis(res, t, counterparty_type=None):
    for rx in _COST_PLUS:
        m = rx.search(t)
        if m:
            res.rate_basis = "Cost-plus"
            res.admin_rate_pct = float(m.group("pct"))
            res.rate_basis_evidence = t[max(0, m.start() - 60):m.end() + 20]
            return
    tiers = [ln for ln in res.lines if ln.tier_min and ln.tier_min > 1 or ln.tier_max]
    if _TIERED.search(t) and tiers:
        res.rate_basis = "Tiered"
        res.rate_basis_evidence = "tier lines " + ", ".join(
            "{}{}".format(ln.tier_min, "-{}".format(ln.tier_max) if ln.tier_max else "+") for ln in tiers)
        return
    pm = _PURCHASED.search(t) if counterparty_type == "Customer" else None
    if pm and re.search(r"(?:monitoring|service)\s+(?:fee|rate)|per\s+day", t, re.I):
        res.rate_basis = "Purchased units"
        res.rate_basis_evidence = t[max(0, pm.start() - 60):pm.end() + 60]


_SPARE_PCT = [
    re.compile(r"shelf\s+units[^%]{0,200}?exceeds?\s+(?P<pct>\d{1,2})\s*%", re.I),
    re.compile(r"(?:spare|shelf)[^.]{0,120}?above\s+(?P<pct>\d{1,2})\s*%", re.I),
    re.compile(r"above\s+(?P<pct>\d{1,2})\s*%\s*(?:spare|shelf|inventory)", re.I),
    re.compile(r"(?:spare|shelf)[^.]{0,80}?(?:from\s+\d{1,2}\s*%\s*)?to\s+(?P<pct>\d{1,2})\s*%", re.I),
    re.compile(r"(?:spare|shelf)[^.]{0,80}?(?P<pct>\d{1,2})\s*%", re.I),
    re.compile(r"(?P<pct>\d{1,2})\s*%\s*(?:of\s+)?(?:active\s+units\s+)?(?:as\s+)?(?:spares?|shelf)", re.I),
]
_SPARE_FEE = re.compile(r"(?:spare|shelf)[^.]{0,300}?\$(?P<fee>\d+\.\d{2})\s*(?:per|/)\s*(?:(?:device|unit)\s+per\s+)?(?:day|unit|device)", re.I)


def _spares(res, t):
    for rx in _SPARE_PCT:
        m = rx.search(t)
        if m:
            res.spare_pct = int(m.group("pct"))
            res.spare_evidence = t[max(0, m.start() - 20):m.end() + 140].strip()
            break
    fm = _SPARE_FEE.search(t)
    if fm:
        res.spare_fee = float(fm.group("fee"))


# ---------------------------------------------------------------------------------------------
# Packet level
# ---------------------------------------------------------------------------------------------

def resolve_current(all_lines):
    """Later-document-wins per device+tier+ownership.

    Returns (rows, superseded):
      rows       -- every distinct line that should exist as a Rate_Lines row: the current line for each
                    group plus the superseded history (an identical amount restated by a later document
                    collapses into the earliest statement)
      superseded -- [(old_line, new_line)] pairs; writeback sets old.Superseded_By = new row id
    Lines without an effective date sort first (treated as oldest); ties keep packet order."""
    groups = {}
    for i, ln in enumerate(all_lines):
        groups.setdefault((ln.device, ln.tier_min or 0, ln.ownership), []).append((ln.effective_date or "", i, ln))
    rows, superseded = [], []
    for items in groups.values():
        items.sort(key=lambda x: (x[0], x[1]))
        chain = []
        for _, _, ln in items:
            if chain and chain[-1].amount == ln.amount:
                continue
            chain.append(ln)
        rows.extend(chain)
        superseded.extend(zip(chain, chain[1:]))
    return rows, superseded


def is_current(line, superseded):
    return all(old is not line for old, _ in superseded)


def packet_pricing(doc_results):
    """doc_results: list of (effective_date, file_name, DocRates). Latest document wins for rate basis
    / admin % and for the spare rule. Returns a dict."""
    ordered = sorted(doc_results, key=lambda x: x[0] or "")
    out = {"rate_basis": None, "admin_rate_pct": None, "rate_basis_evidence": "", "rate_basis_file": None,
           "spare_pct": None, "spare_fee": None, "spare_file": None, "spare_eff": None, "spare_evidence": "",
           "notes": []}
    for eff, fname, dr in ordered:
        if dr.rate_basis:
            out.update(rate_basis=dr.rate_basis, admin_rate_pct=dr.admin_rate_pct,
                       rate_basis_evidence=dr.rate_basis_evidence, rate_basis_file=fname)
        if dr.spare_pct is not None:
            out.update(spare_pct=dr.spare_pct, spare_fee=dr.spare_fee, spare_file=fname, spare_eff=eff,
                       spare_evidence=dr.spare_evidence)
        out["notes"].extend(dr.notes)
    return out

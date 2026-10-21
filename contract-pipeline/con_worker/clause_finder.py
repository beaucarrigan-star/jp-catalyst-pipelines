# clause_finder.py -- Phase 3d: finds the operational obligations in a contract packet and proposes them
# as Contract_Clauses rows. Rule-based, no AI tokens at runtime (Phase 0 decision).
#
# Design (calibrated on the 6 golden packets, 28 Sep 2026 -- see the Phase 3d design note):
#
#   WHAT TO SURFACE  -- a sentence is a clause candidate when (a) it carries an obligation modal
#   ("shall", "must", "will", "agrees to/that", "is responsible for", "is required to") and (b) the
#   subject is one of the contract's parties. It is scored on WHO owes it (JusticePoint's own duties
#   score highest), WHAT KIND of duty it is (the Clause_Type dictionary below), and HOW SPECIFIC it is
#   (a cadence, a deadline, a named role, a count). Score >= HIGH_AT -> High, >= MEDIUM_AT -> Medium,
#   below -> dropped (counted in the Note, never written).
#
#   WHAT TO SUPPRESS -- (1) boilerplate legal sections (indemnification, limitation of liability,
#   arbitration, assignment, force majeure, notices, headings, entire agreement, warranties,
#   definitions, generic confidentiality); (2) anything Phase 3b/3c already turned into a structured
#   field (rate lines, spare-inventory rule, term dates, auto-renewal, payment days, invoice cadence,
#   governing law, insurance limit, not-to-exceed) -- the sentence is suppressed when that is ALL it
#   says; (3) signature blocks, "remaining terms unchanged", pricing tables, addresses; (4) near-
#   duplicates within a packet (same Clause_Type, token overlap >= NEAR_DUP) -- the later document wins.
#
#   The per-document cap (MAX_PER_DOC) is the last defence against a long master agreement burying the
#   6 real duties under 40 "Partner shall" sentences: only the top-scored survive.
#
# Nothing here talks to Zoho. main.py feeds it PacketDocs and hands plan_clauses() the existing rows.

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from typing import List

from text_norm import NUM_RE, normalise, page_of, parse_num, split_pages, source_ref

# ---------------------------------------------------------------------------------------------
# Thresholds (the calibration knobs -- change these before touching the dictionary)
# ---------------------------------------------------------------------------------------------
HIGH_AT = 8          # score at which a proposal is Confidence = High
MEDIUM_AT = 6        # ... Medium; below this the sentence is dropped
MAX_PER_DOC = 12     # top-N per document after scoring
NEAR_DUP = 0.55      # token Jaccard above which two same-type proposals in a packet are one clause
MAX_TEXT = 900       # Clause_Text cap (the textarea is large; the reviewer isn't)

# ---------------------------------------------------------------------------------------------
# Party roles: who is "us"
# ---------------------------------------------------------------------------------------------
US_NAMES = re.compile(r"justice\s*point|justicepoint", re.I)
# '... JusticePoint, Inc. ("Contractor")' / '(hereinafter "Channel Partner")' / '("Partner")'
_DEFINED_ROLE = re.compile(
    r'(?:justice\s*point|justicepoint)[^()]{0,160}?\(\s*(?:hereinafter\s+)?(?:referred\s+to\s+as\s+)?(?:the\s+)?'
    r'["“]?\s*([A-Z][A-Za-z ]{2,30}?)\s*["”]?\s*\)', re.I)
ROLE_VOCAB = {"contractor", "provider", "vendor", "company", "consultant", "service provider", "channel partner",
              "partner", "customer", "reseller", "distributor", "licensee", "client", "supplier", "seller", "buyer",
              "purchaser", "lessee", "lessor", "dealer", "agent", "service partner", "licensor", "manufacturer"}
_QUOTED_TERM = re.compile(r'["“]\s*([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?)\s*["”]')
_OUR_ROLE_DEFAULT = {"Vendor": ("channel partner", "partner", "service partner", "customer", "reseller", "distributor",
                                "dealer", "buyer", "purchaser", "lessee", "licensee"),
                     "Customer": ("contractor", "provider", "vendor", "company", "consultant", "service provider")}
# Vendor names as they appear as a sentence subject. Phase 7 backlog: add each new vendor master as a
# fixture and check party_roles() reads its own ("X") definition before relying on this list.
_THEIR_ROLE_DEFAULT = {"Vendor": ("contractor", "ams", "allied", "attenti", "scram", "securus", "buddi",
                                  "supercom", "track group", "geosatis", "sentinel", "bi incorporated",
                                  "satellite tracking", "corrections corporation", "company", "manufacturer",
                                  "licensor", "lessor", "supplier", "seller", "vendor", "provider"),
                       "Customer": ("county", "city", "state", "customer", "client", "agency", "court",
                                    "department", "school", "district", "sheriff", "purchaser")}

MODAL = re.compile(
    r"\b(shall|must|will|agrees?\s+(?:to|that)|is\s+(?:solely\s+)?responsible\s+for|are\s+responsible\s+for|"
    r"is\s+required\s+to|are\s+required\s+to|may\s+not|shall\s+not|will\s+not)\b", re.I)

# ---------------------------------------------------------------------------------------------
# Clause_Type dictionary. Each type: (keywords that vote for it, weight). The type with the most votes
# wins; ties go to the earlier entry. Keywords are matched as whole words/phrases, case-insensitive.
# ---------------------------------------------------------------------------------------------
TYPE_DICT = [
    ("Reporting", [
        r"report(?:s|ing)?\b", r"utilization", r"statistic", r"itemized", r"itemised", r"audit",
        r"records?\s+retention", r"retain\s+(?:all\s+)?records", r"log(?:s)?\b", r"summary\s+of",
        r"separate\s+(?:monthly\s+)?invoice", r"invoice[s]?\s+(?:shall|will|must)\s+(?:list|specify|include|be\s+itemi[sz]ed)",
        r"with\s+the\s+vendor\s+invoices\s+attached", r"notify|notification|notice\s+of\s+(?:any|each)"]),
    ("Staffing", [
        r"on-?site", r"designated?\s+(?:a\s+)?(?:person|staff|employee|contact|representative)",
        r"staff(?:ing)?\b", r"personnel", r"sufficient\s+number\s+of", r"employees?\b", r"supervis",
        r"business\s+hours", r"24/7|twenty-?\s*four\s*\(?24\)?\s*hours", r"point\s+of\s+contact", r"background\s+check"]),
    ("Service level", [
        r"available\s+(?:for\s+)?twenty|24\s*hours\s+a\s+day", r"response\s+time", r"within\s+\S+\s+(?:hours|business\s+days)\s+of",
        r"maintenance\s+and\s+repair", r"repair", r"replace(?:ment)?\s+(?:the\s+)?(?:product|equipment|device|unit)",
        r"uptime", r"service\s+level", r"technical\s+support", r"help\s*desk", r"alert(?:s)?\s+(?:shall|will|must)",
        r"monitoring\s+services?\s+(?:shall|will|must)"]),
    ("Training", [
        r"train(?:ed|ing)\b", r"certif(?:ied|ication)", r"elearning|e-learning", r"course", r"instruct"]),
    ("Install", [
        r"install(?:ation|ed|s)?\b", r"enroll(?:ment)?", r"hook-?up", r"remov(?:e|al)\s+of\s+(?:the\s+)?(?:device|equipment|bracelet)",
        r"return(?:ed)?\s+(?:the\s+)?(?:equipment|device|units?)", r"ship(?:ping|ment)", r"inventory"]),
    ("Insurance", [
        r"certificate\s+of\s+insurance", r"additional\s+insured", r"notice\s+of\s+cancellation",
        r"cancellation\s+of\s+(?:any\s+)?(?:policy|coverage)", r"workers'?\s+compensation", r"insurance\b"]),
    ("Data & privacy", [
        r"hipaa", r"cjis", r"confidential(?:ity)?\b", r"personal\s+(?:data|information)", r"pii\b", r"phi\b",
        r"data\s+(?:security|protection|breach|retention)", r"privacy", r"security\s+(?:of|for)\s+(?:the\s+)?(?:data|software|information)",
        r"destroy|destruction\s+of\s+(?:the\s+)?(?:data|records)"]),
    ("Termination", [
        r"terminat(?:e|ion)", r"cure\s+period", r"breach", r"effect\s+of\s+termination", r"transition\s+(?:period|services|assistance)",
        r"wind[- ]?down"]),
    ("Special work", [
        r"not\s+eligible", r"excluded?\s+from", r"specific\s+to\s+the\s+program", r"cap\s+of", r"allowance",
        r"lost[,/]?\s*(?:damaged)?[,/]?\s*(?:and\s+|or\s+)?stolen", r"program\b", r"exception", r"pilot", r"minimum\s+(?:order|commitment|quantity)",
        r"exclusiv", r"territory", r"subcontract", r"deliverable", r"milestone", r"scope\s+of\s+work"]),
]
_TYPE_RES = [(t, [re.compile(r"\b(?:" + k + r")", re.I) for k in kws]) for t, kws in TYPE_DICT]

# ---------------------------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------------------------
# Section headings that are boilerplate wherever they occur. A candidate under one of these is dropped.
BOILERPLATE_SECTIONS = re.compile(
    r"^(?:mutual\s+|partner\s+)?(?:indemnif|limitation\s+of\s+liab|liability|arbitration|injunctive|applicable\s+law|"
    r"governing\s+law|assignment|independent\s+contractor|force\s+majeure|notices?$|publicity|headings|execution|"
    r"counterparts|entire\s+agreement|severab|waiver|survival|definitions?|interpretation|"
    r"disclaimers?\s+of\s+warrant|warrant|firmware\s+licen|licen[sc]e\s+grant|intellectual\s+property|"
    r"exceptions\s+from\s+confidential|required\s+disclosures|nondisclosure|non-?disclosure|"
    r"recitals?|purpose|appointment|business\s+classification|advertising|non\s*-?\s*discrimination|"
    r"taxes|compliance\s+with\s+laws|amendment|modification|relationship\s+of\s+the\s+parties|"
    r"third[- ]party\s+beneficiar|dispute|venue|jurisdiction|attorney|remedies|equitable|no\s+waiver)", re.I)

FEES_SECTION = re.compile(r"\b(?:fees?|pricing|prices?|payment|compensation|cost|rental\s+products|orders|freight)\b", re.I)

# Sentences that are only what 3b/3c already captured as a field. Each pattern names the field.
STRUCTURED_ALREADY = [
    ("Rate line", re.compile(r"\$\s?\d[\d,]*(?:\.\d\d)?\s*(?:per|/)\s*(?:unit|participant|device|day|month)", re.I)),
    ("Spare rule (3b)", re.compile(r"spare\s+(?:units?|inventory)", re.I)),
    ("Term dates (3c)", re.compile(r"\b(?:term\s+of\s+this\s+agreement|shall\s+(?:commence|be\s+in\s+effect|continue)\s+(?:on|from)|"
                                   r"initial\s+term|commence\s+on|shall\s+begin\s+on)\b", re.I)),
    ("Auto-renew (3c)", re.compile(r"automatic(?:ally)?\s+renew|renewal\s+term|no\s+automatic\s+renewal|extend(?:ed)?\s+(?:the\s+agreement\s+)?"
                                   r"(?:only\s+)?by\s+(?:mutual\s+)?written", re.I)),
    ("Payment days (3c)", re.compile(r"\bpay\b[^.;]{0,60}?within\s+\S+(?:\s*\(\d+\))?\s*days", re.I)),
    ("Invoice cadence (3c)", re.compile(r"\binvoice\s+(?:the\s+\w+\s+)?(?:monthly|quarterly|annually)\b|submit\s+invoices\s+monthly\b", re.I)),
    ("Governing state (3c)", re.compile(r"governed\s+by\s+(?:and\s+construed\s+(?:in\s+accordance\s+with\s+)?)?the\s+laws", re.I)),
    ("Insurance limit (3c)", re.compile(r"liability\s+insurance\s+(?:of|with\s+limits?\s+of|in\s+the\s+amount\s+of)\s+(?:not\s+less\s+than\s+|at\s+least\s+)?\$", re.I)),
    ("Not-to-exceed (3c)", re.compile(r"shall\s+not\s+exceed\s+\$", re.I)),
    ("Termination notice (3c)", re.compile(r"terminat\w*\s+(?:this\s+agreement\s+)?(?:for\s+convenience|without\s+cause)\s+upon\s+\S+(?:\s*\(\d+\))?\s*days", re.I)),
]
# ... but a sentence that ALSO carries one of these keeps an obligation worth a clause row.
STRUCTURED_EXTRA = re.compile(
    r"certificate\s+of\s+insurance|additional\s+insured|notice\s+of\s+cancellation|prior\s+written\s+notice\s+of\s+cancellation|"
    r"itemi[sz]ed|attached|report|transition|return\s+(?:of\s+)?(?:all\s+)?(?:equipment|devices)|on-?site|designated", re.I)

NOISE = re.compile(
    r"in\s+witness\s+whereof|remaining\s+terms\s+and\s+prices|shall\s+remain\s+unchanged|remain\s+in\s+(?:full\s+)?force|"
    r"intentionally\s+left\s+blank|signature\s+page|^by:|printed\s+title|hereinafter|same\s+meaning\s+ascribed|"
    r"\bmeans\b\s+(?:any|all|the|collectively|a\b)|is\s+entered\s+into|entered\s+into\s+as\s+of|incorporated\s+by\s+reference|"
    r"page\s+\d+\s+of\s+\d+|\bID:\s+[0-9a-f-]{20,}", re.I)

SPECIFICITY = re.compile(
    r"\b(?:monthly|weekly|daily|quarterly|annual(?:ly)?|each\s+month|per\s+month|within\s+\S+(?:\s*\(\d+\))?\s*(?:days|hours|business\s+days)|"
    r"by\s+the\s+\w+\s+(?:business\s+)?day|no\s+later\s+than|prior\s+to|before\s+the\s+end|at\s+least\s+\S+|"
    r"on-?site|designated?|24/7|business\s+hours)\b|\d+\s*%|\(\d+\)", re.I)
SOFT_NOISE = re.compile(r"hereby\s+agree\s+to\s+(?:add|amend|revise|delete)|agree\s+to\s+(?:add\s+)?the\s+following|"
                        r"is\s+(?:hereby\s+)?(?:added|amended|deleted|replaced)|revise\s+the|at\s+the\s+(?:specific\s+)?pricing\s+listed", re.I)
MONEY_CELLS = re.compile(r"\$\s?\d[\d,]*(?:\.\d\d)?")
EXCEPTION = re.compile(r"\bnot\s+eligible|\bexclud(?:ed|es)\s+from|\bexception\b|\bnotwithstanding\b|\bshall\s+not\s+be\s+(?:counted|applied|eligible)", re.I)
CADENCE = re.compile(r"\b(monthly|weekly|daily|quarterly|annually|annual|each\s+month|per\s+month|per\s+quarter|"
                     r"within\s+\S+(?:\s*\(\d+\))?\s*(?:business\s+)?(?:days|hours)|by\s+the\s+\w+\s+(?:business\s+)?day\s+of\s+each\s+month|"
                     r"once\s+per\s+year|not\s+more\s+than\s+once\s+per\s+year)\b", re.I)

# Section heading: "4.5 Customer Information and Satisfaction." / "Article 4. Cost." / "5. Staffing."
HEADING = re.compile(
    r"(?:(?<=\s)|^)(?<!Exhibit )(?<!Addendum )(?<!Amendment )(?<!Page )(?<!No\. )(?<!No )"
    r"(?:(?:Article|Section|ARTICLE|SECTION)\s+)?(\d{1,2}(?:\.\d{1,2}){0,2})\.?\s{1,3}"
    r"([A-Z][A-Za-z&/,'\- ]{2,70}?)\s*[.:](?=\s)")
_STOP = {"and", "of", "the", "to", "for", "with", "or", "in", "on", "by", "a", "an", "&"}


def _is_heading(title):
    words = title.split()
    if not 1 <= len(words) <= 8:
        return False
    return all(w.lower() in _STOP or w[:1].isupper() for w in words)


@dataclass
class ClauseProposal:
    clause_type: str
    title: str
    text: str
    source: str
    confidence: str
    score: int
    obligor: str            # "us" / "them" / "both" / "?"
    cadence: str = ""
    doc_name: str = ""
    section: str = ""
    reasons: List[str] = field(default_factory=list)
    page: int = 0
    due_date: str = ""      # YYYY-MM-DD, only when a rule below can derive it

    def as_note(self):
        return "{} [{}] {} -- {} ({}; {}){}".format(self.clause_type, self.confidence, self.title, self.source,
                                                   self.obligor, ", ".join(self.reasons),
                                                   "; due {}".format(self.due_date) if self.due_date else "")


# ---------------------------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------------------------
def party_roles(text, counterparty_type):
    """Returns (our_roles, their_roles): lower-case defined terms. The doc's own '("Contractor")' style
    definition wins; otherwise the counterparty-type defaults."""
    ours = set()
    for m in _DEFINED_ROLE.finditer(text[:6000]):
        role = m.group(1).strip().lower()
        if role in ROLE_VOCAB:
            ours.add(role)
    ct = "Vendor" if (counterparty_type or "").lower().startswith("vend") else "Customer"
    if not ours:
        defined = {m.group(1).strip().lower() for m in _QUOTED_TERM.finditer(text[:12000])}
        ours = set(_OUR_ROLE_DEFAULT[ct]) & defined or set(_OUR_ROLE_DEFAULT[ct])
    if ours & {"partner", "channel partner", "reseller", "distributor"}:
        ours.discard("customer")   # in a vendor agreement "Customer" is OUR customer, not us
    theirs = set(_THEIR_ROLE_DEFAULT[ct]) - ours
    # any other defined party term in the document ('Buddi US LLC ("Supplier")') is the counterparty
    defined_all = {m.group(1).strip().lower() for m in _QUOTED_TERM.finditer(text[:12000])}
    theirs |= (defined_all & ROLE_VOCAB) - ours
    ours.add("justicepoint")
    ours.add("justice point")
    return ours, theirs


def _obligor(sentence, ours, theirs):
    """Who the modal's subject is. Looks at the words before the first modal."""
    m = MODAL.search(sentence)
    head = sentence[: m.start()].lower() if m else sentence[:80].lower()
    head = head[-120:]
    us = any(re.search(r"\b" + re.escape(r) + r"\b", head) for r in ours)
    them = any(re.search(r"\b" + re.escape(r) + r"\b", head) for r in theirs)
    if re.search(r"\b(?:either|each|both)\s+part(?:y|ies)\b|\bthe\s+parties\b", head):
        return "both"
    if us and them:
        return "both"
    if us:
        return "us"
    if them:
        return "them"
    return "?"


# ---------------------------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------------------------
_NEW_PART = re.compile(r"\b(?:exhibit|schedule|attachment|statement\s+of\s+work|appendix)\b", re.I)


# Unnumbered heading (Allied SOW "Scope & Overview", "Term", "General Information"): a run of 1-4
# Title-Case words at a sentence start, immediately followed by the start of a real sentence -- a party
# term, "This/The/Each/All/Any/Either", or a numbered item. Flattened PDF text has no line breaks to go on.
_HWORD = r"(?:[A-Z][A-Za-z'/\-]*|&|of|and|the|for|to)"
_SENT_START = (r"(?:This|The|These|Each|Either|All|Any|No|In|Unless|Upon|Channel\s+Partner|Partner|Contractor|"
               r"Customer|Vendor|Provider|County|City|Justice\s*Point|JusticePoint|AMS|Allied)\b|\d{1,2}\.\s")
UNNUMBERED_HEADING = re.compile(
    r"(?:^|(?<=[.:;]\s))(" + _HWORD + r"(?:\s" + _HWORD + r"){0,3}?)\s(?=" + _SENT_START + r")")
_HEADING_BANNED = re.compile(r"\b(?:partner|contractor|customer|vendor|provider|county|city|justice|justicepoint|"
                             r"point|ams|allied|channel|agreement|addendum|exhibit|schedule|amendment|sow|per|"
                             r"page|no|dated|between|and|of|the|for|to|&)$", re.I)
_HEADING_FIRST_BANNED = re.compile(r"^(?:the|this|these|each|either|all|any|no|in|unless|upon|per|if|as|by|"
                                   r"with|within|for|and|of|to|&)\b", re.I)


# A one-word unnumbered heading is only believed when it is a word contracts use as a heading.
_ONE_WORD_HEADINGS = {"term", "scope", "overview", "definition", "definitions", "pricing", "invoicing", "billing",
                      "reporting", "staffing", "insurance", "training", "termination", "deliverables",
                      "responsibilities", "purpose", "background", "payment", "services", "equipment",
                      "installation", "support", "maintenance", "confidentiality", "records", "general"}


def _is_unnumbered_heading(title):
    words = title.split()
    if not 1 <= len(words) <= 4 or len(title) > 40:
        return False
    if len(words) == 1 and words[0].lower() not in _ONE_WORD_HEADINGS:
        return False
    if _HEADING_BANNED.search(title) or _HEADING_FIRST_BANNED.search(title):
        return False
    lowered = {w.lower() for w in words}
    if lowered & ROLE_VOCAB or lowered & {"partner", "channel", "justice", "point", "ams", "allied", "county"}:
        return False
    return all(w.lower() in _STOP or w[:1].isupper() for w in words)


def _headings(text):
    """Numbered and unnumbered headings in one position-ordered list of (start, end, title)."""
    found = []
    for h in HEADING.finditer(text):
        t = h.group(2).strip()
        if _is_heading(t):
            found.append((h.start(), h.end(), t))
    taken = [(a, b) for a, b, _ in found]
    for h in UNNUMBERED_HEADING.finditer(text):
        t = h.group(1).strip()
        if not _is_unnumbered_heading(t):
            continue
        if any(a <= h.start(1) < b for a, b in taken):
            continue
        found.append((h.start(1), h.end(), t))
    found.sort()
    return found


def segments(page_texts):
    """Yields (page_no, section_title, sentence, display_text). Sections come from numbered headings
    and short unnumbered Title-Case headings; sentences from a splitter that survives '4.5.1' numbering
    and '(i)' lists. List items under a lead-in ('Partner shall also: 4.5.1 ... 4.5.2 ...') are yielded
    one by one, each scored as '<lead subject + modal> <item>' so the item's own subject is known;
    display_text is the item as written, with its number."""
    section = ""
    for page_no, raw in enumerate(page_texts, start=1):
        text = normalise(raw)
        if not text:
            continue
        if page_no > 1 and _NEW_PART.search(text[:250]):
            section = ""
        pos = 0
        chunks = []
        for start, end, title in _headings(text):
            if start < pos:
                continue
            if start > pos:
                chunks.append((section, text[pos:start]))
            section = title
            pos = end
        chunks.append((section, text[pos:]))
        for sec, body in chunks:
            for s in _sentences(body):
                for scored, shown in _list_items(s):
                    yield page_no, sec, scored, shown


_ITEM_NO = re.compile(r"(?:(?<=\s)|^)(\d{1,2}\.\d{1,2}\.\d{1,2}|\([a-z]\)|\((?:i|ii|iii|iv|v|vi|vii|viii|ix|x)\))\s+(?=[A-Z])")


def _list_items(sentence):
    """'Partner shall also: 4.5.1 Designate ...; 4.5.2 Provide ...' -> one (scored, shown) pair per item.
    A sentence with fewer than two item numbers is returned as it is."""
    marks = list(_ITEM_NO.finditer(sentence))
    if len(marks) < 2:
        return [(sentence, sentence)]
    lead = sentence[:marks[0].start()].strip()
    m = MODAL.search(lead)
    out = []
    if lead and not lead.endswith(":") and len(lead) >= 25:
        out.append((lead, lead))        # a full sentence before the list stands on its own
    prefix = ""
    if m:
        subj = lead[:m.end()]
        subj = re.sub(r"\s+(?:also|further|additionally)\s*$", "", subj, flags=re.I)
        prefix = subj.rsplit(". ", 1)[-1].strip() + " "
    for i, mk in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(sentence)
        item = sentence[mk.end():end].strip()
        item = re.sub(r"[;,]?\s*(?:and|or)?\s*$", "", item).strip(" ;,")
        if len(item) < 15:
            continue
        shown = "{} {}".format(mk.group(1), item)
        if MODAL.search(item) or not prefix:
            scored = item
        else:
            scored = prefix + item[:1].lower() + item[1:]
        out.append((scored, (lead + " " + shown) if lead.endswith(":") and len(lead) <= 60 else shown))
    return out or [(sentence, sentence)]


def _sentences(text):
    # split on ". " / "; " when followed by a capital, digit, or open paren -- but not after "Inc"/"No"/"U.S".
    # "; " before a list number ("AMS; 4.5.2 Provide") splits too, whatever the case of the letter before it.
    parts = re.split(r"(?<!\bInc)(?<!\bNo)(?<!\bU\.S)(?<!\bSec)(?<!\bArt)(?<=[a-z0-9\)\]”\"])\s?[.;][\"”]?\s+(?=[A-Z0-9\(“\"])", text)
    out = []
    for p in parts:
        p = p.strip(" .;")
        if len(p) >= 25:
            out.append(p)
    return out


# ---------------------------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------------------------
def _type_votes(sentence):
    votes = {}
    for t, res in _TYPE_RES:
        n = sum(1 for r in res if r.search(sentence))
        if n:
            votes[t] = n
    return votes


def score_sentence(sentence, section, ours, theirs, doc_type):
    """Returns (score, clause_type, obligor, reasons) or None when suppressed."""
    if not MODAL.search(sentence):
        return None
    if NOISE.search(sentence):
        return None
    if section and BOILERPLATE_SECTIONS.search(section):
        return None
    if section and FEES_SECTION.search(section) and not (STRUCTURED_EXTRA.search(sentence) or EXCEPTION.search(sentence)):
        return None
    already = [name for name, r in STRUCTURED_ALREADY if r.search(sentence)]
    if already and not STRUCTURED_EXTRA.search(sentence):
        return None
    votes = _type_votes(sentence)
    obligor = _obligor(sentence, ours, theirs)
    reasons = []
    score = 0
    if obligor == "us":
        score += 3; reasons.append("our duty")
    elif obligor == "both":
        score += 2; reasons.append("mutual")
    elif obligor == "them":
        score += 1; reasons.append("their duty")
    else:
        score -= 1; reasons.append("subject unclear")
    if votes:
        ctype = max(votes, key=lambda t: (votes[t], -[x for x, _ in TYPE_DICT].index(t)))
        n = votes[ctype]
        score += min(3, 1 + n); reasons.append("{} x{}".format(ctype, n))
    else:
        ctype = "Other"
    if SPECIFICITY.search(sentence):
        score += 2; reasons.append("specific")
    if EXCEPTION.search(sentence):
        score += 2; reasons.append("exception")
    if SOFT_NOISE.search(sentence):
        score -= 2; reasons.append("amendment mechanics")
    if len(MONEY_CELLS.findall(sentence)) >= 2 and not STRUCTURED_EXTRA.search(sentence):
        return None
    if doc_type in ("Addendum", "Amendment"):
        score += 1; reasons.append("addendum")
    # their duties only matter when they are service commitments to us
    if obligor == "them" and ctype in ("Service level", "Training", "Install"):
        score += 2; reasons.append("service commitment to us")
    elif obligor == "them" and ctype not in ("Reporting", "Special work"):
        score -= 2; reasons.append("their non-service duty")
    if ctype == "Other":
        score -= 1
    if already:
        reasons.append("also " + "/".join(already))
    return score, ctype, obligor, reasons


def _title(section, sentence, ctype):
    m = MODAL.search(sentence)
    gist = sentence[m.end():] if m else sentence
    gist = re.sub(r"^\W+", "", gist)
    gist = re.sub(r"^(?:also|further|additionally|in\s+addition)[:,]?\s*", "", gist, flags=re.I)
    gist = re.sub(r"^\d+(?:\.\d+)*\.?\s+", "", gist)
    words = gist.split()
    gist = " ".join(words[:9])
    gist = re.sub(r"[,;:(]\s*$", "", gist).strip()
    head = section if section and len(section) <= 40 else ctype
    return "{}: {}".format(head, gist)[:120] if gist else head[:120]


def clause_hash(contract_id, clause_text):
    normalised = " ".join((clause_text or "").split()).lower()
    return hashlib.sha1((str(contract_id) + "|" + normalised).encode("utf-8")).hexdigest()


def _tokens(s):
    return set(re.findall(r"[a-z]{3,}", s.lower()))


def _merge_sections(props):
    """One clause per (page, section, type): the sentences of a numbered section that all say the same
    kind of thing (three insurance sentences under 4.10) become one row, in document order, capped at
    MAX_TEXT. Score/confidence/title come from the strongest sentence; the cadence from the first that
    has one. Sentences with no section heading are left as they are."""
    groups, order = {}, []
    for p in props:
        key = (p.page, p.section, p.clause_type) if p.section else id(p)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(p)
    out = []
    for key in order:
        members = groups[key]
        if len(members) == 1:
            out.append(members[0])
            continue
        best = max(members, key=lambda p: p.score)
        text = " ".join(m.text for m in members)
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT - 3].rsplit(" ", 1)[0] + "..."
        merged = ClauseProposal(
            clause_type=best.clause_type, title=best.title, text=text, source=best.source,
            confidence=best.confidence, score=best.score, obligor=best.obligor,
            cadence=next((m.cadence for m in members if m.cadence), ""), doc_name=best.doc_name,
            section=best.section, reasons=best.reasons + ["{} sentences merged".format(len(members))],
            page=best.page)
        out.append(merged)
    return out


# ---------------------------------------------------------------------------------------------
# Due_Date: derived only from wording that pins a date down; empty otherwise (a person sets it)
# ---------------------------------------------------------------------------------------------
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
             "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "fifteenth": 15, "twentieth": 20, "last": -1}
_NTH_OF_MONTH = re.compile(r"by\s+the\s+(\w+?)(?:\s*\(\d+(?:st|nd|rd|th)?\))?\s+(business\s+|calendar\s+)?day\s+of\s+"
                           r"(?:each|every|the\s+following)\s+(?:calendar\s+)?month", re.I)
_WITHIN_OF = re.compile(r"within\s+(" + NUM_RE + r")\s*(business\s+|calendar\s+)?days?\s+(?:of|after|following|from)\s+"
                        r"(?:the\s+)?(?:date\s+of\s+)?(?:the\s+)?(effective\s+date\s+of\s+termination|termination|expiration|"
                        r"(?:contract\s+|agreement\s+)?(?:effective|execution|start|commencement)\s+date|execution|commencement)",
                        re.I)


def _d(s):
    try:
        return dt.date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _add_business_days(d, n):
    while n > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _nth_day(year, month, n, business):
    """The Nth (business) day of a month; n = -1 is the last. Weekends only -- no holiday calendar."""
    first = dt.date(year, month, 1)
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    days = [first + dt.timedelta(days=i) for i in range((nxt - first).days)]
    if business:
        days = [x for x in days if x.weekday() < 5]
    return days[n - 1] if n > 0 and n <= len(days) else days[-1]


def derive_due_date(text, start_date=None, end_date=None, today=None):
    """'by the tenth business day of each month' -> the next such day on/after max(start, today);
    'within thirty (30) days of the Effective Date' -> start + 30; '... after termination or expiration'
    -> end + N. Anything else -> ''. Dates are YYYY-MM-DD strings."""
    start, end = _d(start_date), _d(end_date)
    today = _d(today) or dt.date.today()
    m = _NTH_OF_MONTH.search(text or "")
    if m:
        n = _ORDINALS.get(m.group(1).lower()) or (int(m.group(1)) if m.group(1).isdigit() else None)
        if not n:
            return ""
        business = bool(m.group(2) and m.group(2).lower().startswith("business"))
        base = max(x for x in (start, today) if x)
        y, mo = base.year, base.month
        for _ in range(3):
            cand = _nth_day(y, mo, n, business)
            if cand >= base:
                return cand.isoformat()
            y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)
        return ""
    m = _WITHIN_OF.search(text or "")
    if m:
        n = parse_num(m.group(1))
        anchor = end if re.search(r"terminat|expir", m.group(3), re.I) else start
        if not (n and anchor):
            return ""
        business = bool(m.group(2) and m.group(2).lower().startswith("business"))
        due = _add_business_days(anchor, n) if business else anchor + dt.timedelta(days=n)
        return due.isoformat()
    return ""


# ---------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------
def find_clauses(docs, counterparty_type, packet_name="packet.zip", start_date=None, end_date=None, today=None):
    """docs: ordered PacketDocs (effective-date order, from packet_extract). start/end: the contract's
    Contract_Start_Date / Contract_End_Date (for Due_Date). Returns (proposals, dropped_count, notes).
    Proposals are sorted High first, then by score."""
    proposals, notes = [], []
    dropped = 0
    for d in docs:
        if d.doc_type not in ("Base agreement", "Amendment", "Addendum", "Rate sheet"):
            continue
        pages = split_pages(d.text)
        ours, theirs = party_roles(normalise(d.text), counterparty_type)
        per_doc = []
        for page_no, section, sentence, shown in segments(pages):
            r = score_sentence(sentence, section, ours, theirs, d.doc_type)
            if r is None:
                continue
            score, ctype, obligor, reasons = r
            if score < MEDIUM_AT:
                dropped += 1
                continue
            cad = CADENCE.search(sentence)
            per_doc.append(ClauseProposal(
                clause_type=ctype, title=_title(section, sentence, ctype), text=shown[:MAX_TEXT],
                source=source_ref(packet_name, d.name, page_no), confidence="High" if score >= HIGH_AT else "Medium",
                score=score, obligor=obligor, cadence=(cad.group(0) if cad else ""), doc_name=d.name,
                section=section, reasons=reasons, page=page_no))
        per_doc = _merge_sections(per_doc)
        per_doc.sort(key=lambda p: -p.score)
        if len(per_doc) > MAX_PER_DOC:
            notes.append("{}: {} clause candidates, kept the top {}".format(d.name, len(per_doc), MAX_PER_DOC))
            dropped += len(per_doc) - MAX_PER_DOC
            per_doc = per_doc[:MAX_PER_DOC]
        proposals.extend(per_doc)
    # near-duplicate collapse across the packet: later document (later in `docs`) wins
    kept = []
    for p in reversed(proposals):
        toks = _tokens(p.text)
        dup = False
        for k in kept:
            if k.clause_type == p.clause_type:
                inter = len(toks & _tokens(k.text)); union = len(toks | _tokens(k.text)) or 1
                if inter / union >= NEAR_DUP:
                    dup = True
                    break
        if dup:
            dropped += 1
            continue
        kept.append(p)
    kept.reverse()
    kept.sort(key=lambda p: (p.confidence != "High", -p.score))
    for p in kept:
        p.due_date = derive_due_date(p.text, start_date, end_date, today)
    return kept, dropped, notes


def plan_clauses(existing_rows, proposals, contract_id, account_id=None):
    """existing_rows: Contract_Clauses rows already on this Contract (dicts with Clause_Hash, Status).
    Returns (plan, notes): plan = {"create": [fields...]}. A row whose hash exists is skipped; a Rejected
    row is never re-proposed (Phase 0 rule)."""
    by_hash = {r.get("Clause_Hash"): r for r in (existing_rows or [])}
    plan = {"create": []}
    notes = []
    for p in proposals:
        h = clause_hash(contract_id, p.text)
        row = by_hash.get(h)
        if row:
            if row.get("Status") == "Rejected":
                notes.append("{}: previously rejected, not re-proposed".format(p.title))
            continue
        fields = {
            "Name": p.title[:120],          # the module's record-name field (mandatory, 120 chars)
            "Contract": {"id": contract_id},
            "Clause_Type": p.clause_type,
            "Title": p.title[:255],
            "Clause_Text": p.text,
            "Source_Ref": p.source[:255],
            "Cadence": p.cadence[:255],
            "Status": "Proposed",
            "Confidence": p.confidence,
            "Clause_Hash": h,
        }
        if p.due_date:
            fields["Due_Date"] = p.due_date
        if account_id:
            fields["Account"] = {"id": account_id}
        plan["create"].append(fields)
    return plan, notes

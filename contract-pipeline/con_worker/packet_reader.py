# packet_reader.py
# Vendored from the solicitation-pipeline zip ingest (claude/solicitation-pipeline-catalyst-live-2026-09-25.md)
# with the same limits, so the two pipelines behave identically for the part Beau already trusts.
#
# Opens a packet .zip attached to a Contract (or, for the "new contract from packet" path, an Account)
# and returns one entry per readable file: name, extension, raw bytes, extracted text (for classification
# and no-text-layer detection), page count (PDFs), and a sha1 of the bytes (used as the Contract_Documents
# upsert key together with File_Name).
#
# 28 Sep 2026 (Phase 3b/3c): PDF pages are now joined with a form feed (\f) instead of \n so the rate
# and terms extractors can cite the page a value came from ("packet.zip > Addendum 24.pdf, p.2").
#
# Skipped, and named in the caller's summary: zips inside the zip, password-protected files, file types
# other than pdf/docx/xlsx/xls/csv/txt, __MACOSX entries, and hidden files (dotfiles).
# Limits: 200 files, 60 MB per file, 250 MB unzipped in total. These match the solicitation pipeline's
# limits exactly -- do not diverge without updating both.

import hashlib
import io
import logging
import time
import zipfile

for _noisy in ("pdfminer", "pdfplumber", "PIL"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)  # per-token DEBUG logging made big PDFs crawl

MAX_PDF_SECONDS = 120  # per file; a PDF that takes longer is reported by name instead of hanging the job
MIN_PAGE_CHARS = 40  # below this a PDF page is treated as image-only (no text layer)

MAX_FILES = 200
MAX_FILE_BYTES = 60 * 1024 * 1024
MAX_TOTAL_BYTES = 250 * 1024 * 1024

READABLE_EXT = {".pdf", ".docx", ".xlsx", ".xls", ".csv", ".txt"}


class PacketEntry:
    def __init__(self, name, ext, raw_bytes, text, pages, sha1, no_text_layer, skip_reason=None):
        self.name = name
        self.ext = ext
        self.raw_bytes = raw_bytes
        self.text = text
        self.pages = pages
        self.sha1 = sha1
        self.no_text_layer = no_text_layer
        self.skip_reason = skip_reason

    def to_summary(self):
        if self.skip_reason:
            return "{} (skipped: {})".format(self.name, self.skip_reason)
        if self.no_text_layer:
            return "{} (no text layer -- needs OCR on Beau's PC)".format(self.name)
        return self.name


def _is_hidden_or_system(name):
    base = name.rsplit("/", 1)[-1]
    if base == "":
        return True  # directory entry
    if base.startswith("."):
        return True
    if "__MACOSX" in name:
        return True
    return False


def _extract_pdf_text(raw_bytes):
    # pdfplumber is the project's standard PDF reader (see the solicitation and billing pipelines).
    # A page with no extractable text is what "no text layer" means -- those pages were scanned images
    # and need OCR on Beau's PC before re-upload (Phase 0 decision: no OCR at runtime).
    import pdfplumber

    # A page counts as having a text layer only with MIN_PAGE_CHARS+ real characters: a scan whose only
    # text is a date stamp or Bates number (Monroe County OH, 27 Sep 2026: 7 chars over 7 pages) is still
    # a scan. The file needs OCR when fewer than half its pages have text.
    text_parts = []
    pages = 0
    text_pages = 0
    started = time.monotonic()
    with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
        pages = len(pdf.pages)
        for page in pdf.pages:
            if time.monotonic() - started > MAX_PDF_SECONDS:
                raise RuntimeError("took over {}s to read (vector-heavy or very large PDF) -- flatten or OCR it on Beau's PC and re-upload".format(MAX_PDF_SECONDS))
            t = page.extract_text() or ""
            if len("".join(t.split())) >= MIN_PAGE_CHARS:
                text_pages += 1
            text_parts.append(t.replace("\f", " "))
    return "\f".join(text_parts), pages, (text_pages * 2 < max(pages, 1))


def _extract_docx_text(raw_bytes):
    import docx

    d = docx.Document(io.BytesIO(raw_bytes))
    paras = [p.text for p in d.paragraphs]
    for table in d.tables:
        for row in table.rows:
            paras.append("  ".join(cell.text for cell in row.cells))  # keep a row's cells together
    text = "\n".join(paras)
    return text, None, (len(text.strip()) == 0)


def _extract_xlsx_text(raw_bytes):
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True, read_only=True)
    rows_text = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            rows_text.append("\t".join("" if v is None else str(v) for v in row))
    text = "\n".join(rows_text)
    return text, None, (len(text.strip()) == 0)


def _extract_plain_text(raw_bytes):
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = raw_bytes.decode("latin-1", errors="replace")
    return text, None, (len(text.strip()) == 0)


def read_packet(zip_bytes):
    """Returns (entries: list[PacketEntry], packet_sha256: str, warnings: list[str])."""
    warnings = []
    entries = []
    total_bytes = 0
    packet_sha256 = hashlib.sha256(zip_bytes).hexdigest()

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        warnings.append("packet.zip is not a valid zip file")
        return entries, packet_sha256, warnings

    names = zf.namelist()
    if len(names) > MAX_FILES:
        warnings.append(
            "packet.zip has {} files, only the first {} were read".format(len(names), MAX_FILES)
        )
        names = names[:MAX_FILES]

    for name in names:
        if _is_hidden_or_system(name):
            continue
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""

        if ext == ".zip":
            entries.append(PacketEntry(name, ext, None, "", None, None, False, "nested zip, not opened"))
            continue

        info = zf.getinfo(name)
        if info.file_size > MAX_FILE_BYTES:
            entries.append(PacketEntry(name, ext, None, "", None, None, False, "over 60 MB, not read"))
            continue

        if ext not in READABLE_EXT:
            entries.append(PacketEntry(name, ext, None, "", None, None, False, "unsupported file type"))
            continue

        try:
            raw = zf.read(name)
        except RuntimeError as e:
            # zipfile raises RuntimeError for password-protected entries
            if "password" in str(e).lower():
                entries.append(PacketEntry(name, ext, None, "", None, None, False, "password protected"))
                continue
            entries.append(PacketEntry(name, ext, None, "", None, None, False, "could not read: " + str(e)))
            continue

        total_bytes += len(raw)
        if total_bytes > MAX_TOTAL_BYTES:
            entries.append(PacketEntry(name, ext, None, "", None, None, False, "over the 250 MB packet total, not read"))
            continue

        sha1 = hashlib.sha1(raw).hexdigest()

        try:
            if ext == ".pdf":
                text, pages, no_text = _extract_pdf_text(raw)
            elif ext == ".docx":
                text, pages, no_text = _extract_docx_text(raw)
            elif ext in (".xlsx", ".xls"):
                text, pages, no_text = _extract_xlsx_text(raw)
            else:  # .csv, .txt
                text, pages, no_text = _extract_plain_text(raw)
        except Exception as e:  # noqa: BLE001 -- a single bad file must not fail the whole packet
            entries.append(PacketEntry(name, ext, raw, "", None, sha1, False, "could not parse: " + str(e)))
            continue

        entries.append(PacketEntry(name, ext, raw, text, pages, sha1, no_text))

    return entries, packet_sha256, warnings

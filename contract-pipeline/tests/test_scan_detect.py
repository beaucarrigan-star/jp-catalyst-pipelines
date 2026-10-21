# Monroe County OH (27 Sep 2026): a 7-page scan whose only text was a "9/24/24" stamp was read as a
# text PDF and ingested as empty. A page needs MIN_PAGE_CHARS real characters to count as text.
import io, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "con_worker"))
from reportlab.pdfgen import canvas
import packet_reader as pr


def _pdf(pages):
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for txt in pages:
        c.drawString(72, 720, txt)
        c.showPage()
    c.save()
    return buf.getvalue()


BODY = "This Agreement is entered into by Monroe County and JusticePoint for electronic monitoring services."


def test_stamp_only_scan_is_no_text_layer():
    _, pages, no_text = pr._extract_pdf_text(_pdf([""] * 6 + ["9/24/24"]))
    assert pages == 7 and no_text


def test_text_pdf_with_scanned_signature_page_is_readable():
    _, pages, no_text = pr._extract_pdf_text(_pdf([BODY] * 6 + [""]))
    assert pages == 7 and not no_text


def test_mostly_scanned_is_no_text_layer():
    _, _, no_text = pr._extract_pdf_text(_pdf([BODY, BODY] + [""] * 5))
    assert no_text

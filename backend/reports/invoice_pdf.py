"""Small, dependency-free-at-runtime invoice PDF renderer using PyMuPDF.

PyMuPDF is already an EchoSphere dependency for knowledge ingestion, so this
does not add a second PDF stack just for the billing download.
"""

from datetime import date
from decimal import Decimal

import fitz


def _pdf_text(value: object) -> str:
    # The built-in Helvetica font is Latin-1. Replacing unsupported glyphs
    # keeps the PDF valid without embedding a new font dependency.
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text.encode("latin-1", "replace").decode("latin-1")


def render_invoice_pdf(
    *,
    invoice_id: str,
    tenant_name: str,
    period: str,
    amount: Decimal | float,
    status: str,
    issued_at: date | None,
) -> bytes:
    document = fitz.open()
    try:
        page = document.new_page(width=595, height=842)
        page.insert_text((54, 72), "AUREXION EchoSphere", fontsize=18)
        page.insert_text((54, 102), "Invoice summary", fontsize=14)
        lines = (
            ("Invoice ID", invoice_id),
            ("Tenant", tenant_name),
            ("Billing period", period),
            ("Amount", f"${float(amount):,.2f}"),
            ("Status", status.replace("_", " ").title()),
            ("Issued date", issued_at.isoformat() if issued_at else "Not specified"),
        )
        y = 148
        for label, value in lines:
            page.insert_text((54, y), _pdf_text(f"{label}:"), fontsize=10)
            page.insert_text((180, y), _pdf_text(value), fontsize=10)
            y += 28
        page.insert_text(
            (54, 790),
            "Generated from EchoSphere billing records.",
            fontsize=8,
            color=(0.35, 0.35, 0.35),
        )
        return document.tobytes(garbage=4, deflate=True)
    finally:
        document.close()

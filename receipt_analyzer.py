"""
Receipt reading. Two methods, both free:

1. Gemini (Google's free AI API) - used automatically if GEMINI_API_KEY is
   set. Reads the receipt like a person would: understands context, not
   just raw text, so it's far more reliable than plain OCR (correctly
   skips SUBTOTAL vs TOTAL, handles messy layouts, unusual date formats,
   handwriting, etc).
2. Tesseract OCR + regex guessing - the original free-forever fallback,
   used automatically if no Gemini key is set, or if a Gemini call fails
   for any reason (rate limit, network blip, etc). Plain text extraction,
   less reliable on messy receipts, but needs no account at all.

Either way, the member always sees the result and can correct it before
submitting, so a bad read from either method is never final.
"""
import io
import re
import logging
from datetime import datetime
from typing import List, Optional

from PIL import Image
import pytesseract

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Method 1: Gemini (used when GEMINI_API_KEY is configured)
# ---------------------------------------------------------------------------

def _analyze_with_gemini(image_bytes: bytes) -> Optional[dict]:
    if not config.GEMINI_API_KEY:
        return None

    try:
        import base64
        from google import genai
        from pydantic import BaseModel, Field

        class ReceiptData(BaseModel):
            vendor: Optional[str] = Field(description="Store or merchant name, or null if unreadable")
            date: Optional[str] = Field(description="Purchase date in MM/DD/YY format, or null if unreadable")
            total_amount: Optional[str] = Field(description="The final total paid, as a plain number like '24.99', or null")
            tax: Optional[str] = Field(description="Sales tax amount as a plain number, or null if not shown")
            items: List[str] = Field(default_factory=list, description="Short description of each line item, max 8")
            confidence: str = Field(description="'high', 'medium', or 'low' confidence in the total_amount and date")
            notes: Optional[str] = Field(description="Anything unusual worth flagging to a human reviewer, or null")

        client = genai.Client(api_key=config.GEMINI_API_KEY)

        prompt = (
            "You are reading a photo of a purchase receipt for a college club "
            "reimbursement request. Extract the vendor, purchase date, total "
            "amount, tax, and up to 8 line items. If the image is not a "
            "receipt at all, set the fields to null and explain in notes."
        )

        interaction = client.interactions.create(
            model="gemini-3.7-flash",
            input=[
                {"type": "text", "text": prompt},
                {
                    "type": "image",
                    "data": base64.b64encode(image_bytes).decode("utf-8"),
                    "mime_type": "image/jpeg",
                },
            ],
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": ReceiptData.model_json_schema(),
            },
            generation_config={"thinking_level": "minimal"},
            timeout=20,  # seconds — fail fast and fall back rather than hang
        )

        parsed = ReceiptData.model_validate_json(interaction.output_text)
        return parsed.model_dump()

    except Exception:
        logger.exception("Gemini receipt analysis failed, falling back to OCR")
        return None


# ---------------------------------------------------------------------------
# Method 2: Tesseract OCR + regex guessing (always-available fallback)
# ---------------------------------------------------------------------------

# The negative lookbehinds keep this from matching inside "SUBTOTAL".
_TOTAL_LINE_RE = re.compile(
    r"(?<!sub)(?<!sub )(?:total|amount\s*due|balance\s*due|grand\s*total)\D{0,10}(\d+\.\d{2})",
    re.IGNORECASE,
)
_TAX_LINE_RE = re.compile(r"(?:tax)\D{0,10}(\d+\.\d{2})", re.IGNORECASE)
_MONEY_RE = re.compile(r"\$?\s?(\d{1,4}\.\d{2})\b")

_DATE_PATTERNS = [
    (re.compile(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})\b"), "mdy_slash"),
    (re.compile(r"\b(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})\b"), "ymd_slash"),
]


def _normalize_date(match, kind: str):
    try:
        if kind == "mdy_slash":
            m, d, y = match.groups()
            y = int(y)
            if y < 100:
                y += 2000
            return datetime(y, int(m), int(d)).strftime("%m/%d/%y")
        else:
            y, m, d = match.groups()
            return datetime(int(y), int(m), int(d)).strftime("%m/%d/%y")
    except ValueError:
        return None


def _guess_date(text: str):
    for pattern, kind in _DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            result = _normalize_date(match, kind)
            if result:
                return result
    return None


def _guess_vendor(lines: list) -> str:
    for line in lines[:5]:
        cleaned = line.strip()
        if len(cleaned) >= 3 and not re.search(r"\d{3,}", cleaned):
            return cleaned
    return lines[0].strip() if lines else "Unknown"


def _guess_total(text: str):
    matches = _TOTAL_LINE_RE.findall(text)
    if matches:
        return matches[-1]
    amounts = [float(m) for m in _MONEY_RE.findall(text)]
    if amounts:
        return f"{max(amounts):.2f}"
    return None


def _guess_tax(text: str):
    match = _TAX_LINE_RE.search(text)
    return match.group(1) if match else None


def _analyze_with_tesseract(image_bytes: bytes) -> dict:
    try:
        image = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(image)
    except Exception as e:
        return {
            "vendor": None, "date": None, "total_amount": None, "tax": None,
            "items": [], "confidence": "low",
            "notes": f"Could not read the image: {e}",
        }

    lines = [l for l in text.splitlines() if l.strip()]
    vendor = _guess_vendor(lines)
    date = _guess_date(text)
    total = _guess_total(text)
    tax = _guess_tax(text)

    confidence = "medium" if (total and date) else "low"
    notes = (
        None if confidence == "medium"
        else "OCR had trouble reading this receipt clearly — please double-check "
        "the fields above carefully before confirming."
    )

    return {
        "vendor": vendor, "date": date, "total_amount": total, "tax": tax,
        "items": [], "confidence": confidence, "notes": notes,
    }


# ---------------------------------------------------------------------------
# Public entry point — tries Gemini first, falls back to Tesseract
# ---------------------------------------------------------------------------

def analyze_receipt(image_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    gemini_result = _analyze_with_gemini(image_bytes)
    if gemini_result is not None:
        return gemini_result
    return _analyze_with_tesseract(image_bytes)

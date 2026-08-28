"""
Free, local receipt reading using Tesseract OCR (no API key, no cost,
no account of any kind). Trade-off vs. an AI vision model: this is plain
text extraction plus pattern-matching, so it's noticeably less reliable on
crumpled, faded, or handwritten receipts. The bot always shows the member
what it found and lets them correct it, so a bad read is never final.
"""
import io
import re
from datetime import datetime
from PIL import Image
import pytesseract

# Matches things like "TOTAL 12.34", "Total: $12.34", "AMOUNT DUE  12.34"
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
    # Receipts almost always put the store name in the first few
    # non-empty, non-numeric lines.
    for line in lines[:5]:
        cleaned = line.strip()
        if len(cleaned) >= 3 and not re.search(r"\d{3,}", cleaned):
            return cleaned
    return lines[0].strip() if lines else "Unknown"


def _guess_total(text: str):
    matches = _TOTAL_LINE_RE.findall(text)
    if matches:
        return matches[-1]
    # Fall back to the largest dollar-looking number on the receipt
    amounts = [float(m) for m in _MONEY_RE.findall(text)]
    if amounts:
        return f"{max(amounts):.2f}"
    return None


def _guess_tax(text: str):
    match = _TAX_LINE_RE.search(text)
    return match.group(1) if match else None


def analyze_receipt(image_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    """Returns the same shape as before so bot.py doesn't need to change:
    vendor, date, total_amount, tax, items, confidence, notes."""
    try:
        image = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(image)
    except Exception as e:
        return {
            "vendor": None,
            "date": None,
            "total_amount": None,
            "tax": None,
            "items": [],
            "confidence": "low",
            "notes": f"Could not read the image: {e}",
        }

    lines = [l for l in text.splitlines() if l.strip()]
    vendor = _guess_vendor(lines)
    date = _guess_date(text)
    total = _guess_total(text)
    tax = _guess_tax(text)

    confidence = "medium" if (total and date) else "low"
    notes = (
        None
        if confidence == "medium"
        else "OCR had trouble reading this receipt clearly — please double-check "
        "the fields above carefully before confirming."
    )

    return {
        "vendor": vendor,
        "date": date,
        "total_amount": total,
        "tax": tax,
        "items": [],
        "confidence": confidence,
        "notes": notes,
    }

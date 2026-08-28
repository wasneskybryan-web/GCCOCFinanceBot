"""
All Google Sheets access. The main sheet is the visible log the treasurer
edits (setting Status). A second, hidden-in-plain-sight tab called
"_Notified Log" tracks which request IDs have already triggered a member
notification, so the bot doesn't re-notify the same person every time it
checks the sheet — without needing a visible "Notified" column on the main
sheet.
"""
import json
import gspread
from google.oauth2.service_account import Credentials

import config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HEADERS = [
    "ID", "Timestamp", "Member Name", "Telegram Name", "Member Chat ID",
    "Category", "Purpose", "Merchant", "Purchase Date", "Amount",
    "Receipt", "Status",
]

NOTIFIED_LOG_TITLE = "_Notified Log"

_credentials = None
_gspread_client = None


def _get_credentials():
    global _credentials
    if _credentials is None:
        info = json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON)
        _credentials = Credentials.from_service_account_info(info, scopes=SCOPES)
    return _credentials


def _get_gspread_client():
    global _gspread_client
    if _gspread_client is None:
        _gspread_client = gspread.authorize(_get_credentials())
    return _gspread_client


def get_worksheet():
    gc = _get_gspread_client()
    sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
    ws = sh.sheet1
    first_row = ws.row_values(1)
    if first_row != HEADERS:
        ws.clear()  # wipes any leftover columns from an old layout too
        ws.update("A1", [HEADERS])
    return ws


def _get_notified_log_worksheet():
    gc = _get_gspread_client()
    sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
    try:
        return sh.worksheet(NOTIFIED_LOG_TITLE)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=NOTIFIED_LOG_TITLE, rows=1000, cols=1)
        ws.update("A1", [["Notified Request IDs"]])
        return ws


def request_code(row_number: int) -> str:
    return f"{config.CLUB_ABBREVIATION}-{row_number}"


def append_submission(data: dict) -> str:
    """Adds a new row and returns its request code (e.g. GCCOC-14)."""
    ws = get_worksheet()
    existing_rows = len(ws.get_all_values())  # includes header row
    row_number = existing_rows
    code = request_code(row_number)

    row = [
        code,
        data.get("created_at", ""),
        data.get("member_name", ""),
        data.get("telegram_name", ""),
        str(data.get("member_chat_id", "")),
        data.get("category", ""),
        data.get("purpose", ""),
        data.get("vendor", ""),
        data.get("date", ""),
        data.get("amount", ""),
        data.get("receipt_note", "Sent via Telegram"),
        "Pending",
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
    return code


def get_unnotified_decisions():
    """Returns [record_dict, ...] for rows where Status is a final decision
    and the request ID isn't already in the hidden notified log."""
    ws = get_worksheet()
    records = ws.get_all_records(expected_headers=HEADERS)

    log_ws = _get_notified_log_worksheet()
    already_notified = set(log_ws.col_values(1)[1:])  # skip header

    final_statuses = {"approved", "denied", "needs more info"}
    results = []
    for record in records:
        status = str(record.get("Status", "")).strip().lower()
        if status in final_statuses and record.get("ID") not in already_notified:
            results.append(record)
    return results


def mark_notified(request_id: str):
    log_ws = _get_notified_log_worksheet()
    log_ws.append_row([request_id], value_input_option="USER_ENTERED")

"""
All Google Sheets access. The Sheet is the visible log the treasurer edits
(setting Status). The Notified column and its red/green cell coloring are
maintained by the bot, but header setup is intentionally hands-off after
the first write — see get_worksheet() — so any manual formatting/styling
you apply to the sheet is never touched or wiped by the bot.
"""
import json
import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials

import config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

HEADERS = [
    "ID", "Timestamp", "Member Name", "Telegram Name", "Member Chat ID",
    "Category", "Purpose", "Merchant", "Purchase Date", "Amount",
    "Receipt", "Status", "Notified",
]

RED = {"red": 0.95, "green": 0.6, "blue": 0.6}
GREEN = {"red": 0.65, "green": 0.9, "blue": 0.65}
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}

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
    """Only writes headers if the sheet is completely blank (brand new).
    Never overwrites or clears an existing header/rows — safe to style,
    reorder, or reformat the sheet by hand without the bot resetting it."""
    gc = _get_gspread_client()
    sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
    ws = sh.sheet1
    first_row = ws.row_values(1)
    if not first_row:
        ws.update("A1", [HEADERS])
    return ws


def request_code(row_number: int) -> str:
    return f"{config.CLUB_ABBREVIATION}-{row_number}"


def append_submission(data: dict) -> str:
    """Adds a new row and returns its request code (e.g. GCCOC-14)."""
    ws = get_worksheet()
    existing_rows = len(ws.get_all_values())  # includes header row
    row_number = existing_rows
    code = request_code(row_number)
    new_row_index = existing_rows + 1  # 1-based sheet row for this new entry

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
        "No",
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")

    notified_col = HEADERS.index("Notified") + 1
    cell = rowcol_to_a1(new_row_index, notified_col)
    ws.format(cell, {"backgroundColor": RED})

    return code


def get_unnotified_decisions():
    """Returns (worksheet, [(row_index, record_dict), ...]) for rows where
    Status is a final decision but Notified isn't yet marked Yes."""
    ws = get_worksheet()
    records = ws.get_all_records(expected_headers=HEADERS)
    results = []
    final_statuses = {"approved", "denied", "needs more info"}
    for i, record in enumerate(records, start=2):  # row 1 is headers
        status = str(record.get("Status", "")).strip().lower()
        notified = str(record.get("Notified", "")).strip().lower()
        if status in final_statuses and notified != "yes":
            results.append((i, record))
    return ws, results


def mark_notified(ws, row_index: int, status: str):
    notified_col = HEADERS.index("Notified") + 1
    ws.update_cell(row_index, notified_col, "Yes")

    cell = rowcol_to_a1(row_index, notified_col)
    color = GREEN if status.strip().lower() == "approved" else WHITE
    ws.format(cell, {"backgroundColor": color})

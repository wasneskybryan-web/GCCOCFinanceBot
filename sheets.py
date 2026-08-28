"""
Reimbursement worksheet (the first/main tab). The Sheet is the visible log
the treasurer edits (setting Status). Header setup is intentionally
hands-off after the first write — see get_worksheet() — so any manual
formatting/styling you apply to the sheet is never touched or wiped.
"""
from datetime import datetime, timedelta
import gspread
from gspread.utils import rowcol_to_a1

import config
from sheets_client import get_gspread_client, get_spreadsheet, RED, GREEN, WHITE

HEADERS = [
    "ID", "Timestamp", "Member Name", "Telegram Name", "Member Chat ID",
    "Category", "Purpose", "Merchant", "Purchase Date", "Amount",
    "Receipt", "Status", "Notified",
]


def get_worksheet():
    """Only writes headers if the sheet is completely blank (brand new).
    Never overwrites or clears an existing header/rows — safe to style,
    reorder, or reformat the sheet by hand without the bot resetting it."""
    sh = get_spreadsheet()
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


# ---------------------------------------------------------------------------
# Stale-request nudge (7+ days pending). Tracked in a hidden tab so it never
# touches the visible sheet or its formatting.
# ---------------------------------------------------------------------------

REMINDER_LOG_TITLE = "_Reminder Log"


def _get_reminder_log_worksheet():
    sh = get_spreadsheet()
    try:
        return sh.worksheet(REMINDER_LOG_TITLE)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=REMINDER_LOG_TITLE, rows=1000, cols=1)
        ws.update("A1", [["Reminded Request IDs"]])
        return ws


def get_stale_pending_requests(min_age_days: int = 7):
    """Returns records still Pending that were submitted at least
    min_age_days ago and haven't already triggered a reminder."""
    ws = get_worksheet()
    records = ws.get_all_records(expected_headers=HEADERS)

    log_ws = _get_reminder_log_worksheet()
    already_reminded = set(log_ws.col_values(1)[1:])  # skip header

    cutoff = datetime.now() - timedelta(days=min_age_days)
    results = []
    for record in records:
        status = str(record.get("Status", "")).strip().lower()
        if status != "pending" or record.get("ID") in already_reminded:
            continue
        try:
            submitted_at = datetime.strptime(record.get("Timestamp", ""), "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if submitted_at <= cutoff:
            results.append(record)
    return results


def mark_reminded(request_id: str):
    log_ws = _get_reminder_log_worksheet()
    log_ws.append_row([request_id], value_input_option="USER_ENTERED")


# ---------------------------------------------------------------------------
# Editing a member's own still-pending request
# ---------------------------------------------------------------------------

def get_member_pending_requests(member_chat_id):
    """Returns [(row_index, record_dict), ...] for this member's requests
    that are still Pending (and therefore still safe to edit)."""
    ws = get_worksheet()
    records = ws.get_all_records(expected_headers=HEADERS)
    chat_id_str = str(member_chat_id)
    results = []
    for i, record in enumerate(records, start=2):
        status = str(record.get("Status", "")).strip().lower()
        if status == "pending" and str(record.get("Member Chat ID")) == chat_id_str:
            results.append((i, record))
    return results


def get_current_status(row_index: int) -> str:
    """Re-checks a row's live Status — used right before applying an edit
    to guard against the treasurer deciding it in the same moment."""
    ws = get_worksheet()
    status_col = HEADERS.index("Status") + 1
    return ws.cell(row_index, status_col).value or ""


def update_field(row_index: int, header: str, value: str):
    ws = get_worksheet()
    col = HEADERS.index(header) + 1
    ws.update_cell(row_index, col, value)


# ---------------------------------------------------------------------------
# Dues / trip payments — a separate tab in the same spreadsheet, since this
# is money owed TO the club rather than an expense to reimburse, with a
# different schema. Same file, same credentials, no new setup needed.
# ---------------------------------------------------------------------------

PAYMENT_SHEET_TITLE = "Dues & Trip Payments"

PAYMENT_HEADERS = [
    "ID", "Timestamp", "Member Name", "Telegram Name", "Member Chat ID",
    "Purpose", "Amount", "Payment Method", "Status", "Notified",
]


def get_payment_worksheet():
    gc = _get_gspread_client()
    sh = gc.open_by_key(config.GOOGLE_SHEET_ID)
    try:
        ws = sh.worksheet(PAYMENT_SHEET_TITLE)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=PAYMENT_SHEET_TITLE, rows=1000, cols=len(PAYMENT_HEADERS))
        ws.update("A1", [PAYMENT_HEADERS])
        return ws

    first_row = ws.row_values(1)
    if not first_row:
        ws.update("A1", [PAYMENT_HEADERS])
    return ws


def payment_request_code(row_number: int) -> str:
    return f"{config.CLUB_ABBREVIATION}-PAY-{row_number}"


def append_payment(data: dict) -> str:
    """Adds a new payment row and returns its request code (e.g. GCCOC-PAY-5)."""
    ws = get_payment_worksheet()
    existing_rows = len(ws.get_all_values())
    row_number = existing_rows
    code = payment_request_code(row_number)
    new_row_index = existing_rows + 1

    row = [
        code,
        data.get("created_at", ""),
        data.get("member_name", ""),
        data.get("telegram_name", ""),
        str(data.get("member_chat_id", "")),
        data.get("purpose", ""),
        data.get("amount", ""),
        data.get("payment_method", ""),
        "Pending",
        "No",
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")

    notified_col = PAYMENT_HEADERS.index("Notified") + 1
    cell = rowcol_to_a1(new_row_index, notified_col)
    ws.format(cell, {"backgroundColor": RED})

    return code


def get_unnotified_payment_decisions():
    """Same idea as get_unnotified_decisions(), but for the payments tab."""
    ws = get_payment_worksheet()
    records = ws.get_all_records(expected_headers=PAYMENT_HEADERS)
    results = []
    final_statuses = {"approved", "denied", "needs more info"}
    for i, record in enumerate(records, start=2):
        status = str(record.get("Status", "")).strip().lower()
        notified = str(record.get("Notified", "")).strip().lower()
        if status in final_statuses and notified != "yes":
            results.append((i, record))
    return ws, results


def mark_payment_notified(ws, row_index: int, status: str):
    notified_col = PAYMENT_HEADERS.index("Notified") + 1
    ws.update_cell(row_index, notified_col, "Yes")

    cell = rowcol_to_a1(row_index, notified_col)
    color = GREEN if status.strip().lower() == "approved" else WHITE
    ws.format(cell, {"backgroundColor": color})

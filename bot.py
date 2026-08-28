"""
Grove City College Outing Club reimbursement Telegram bot.

Member flow:
  /reimburse -> name -> purpose -> category -> upload receipt photo(s)
  -> "done" -> confirm extracted data (or fix it) -> row added to Google
  Sheet, receipt uploaded to Google Drive, treasurer gets a text notification

Treasurer flow:
  Open the Google Sheet, edit the "Status" column for a row to Approved /
  Denied / Needs More Info. The bot checks the Sheet periodically and
  messages the member automatically once it sees a final status.
"""
import logging
import io
import re
from datetime import datetime

from better_profanity import profanity

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import config
import sheets
from receipt_analyzer import analyze_receipt

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

ASK_NAME, ASK_PURPOSE, ASK_CATEGORY, WAITING_PHOTO, CONFIRM_DATA, MANUAL_FIX = range(6)

profanity.load_censor_words()

_PLACEHOLDER_NAMES = {
    "test", "testing", "n/a", "na", "none", "unknown", "idk", "asdf",
    "xxx", "xx", "abc", "name", "your name", "full name", "john doe",
    "jane doe", "first last",
}


# ---------- Input validation for anything a member types by hand ----------

def _validate_name(text: str):
    text = text.strip()
    if len(text) < 2 or len(text) > 60:
        return None, "Please enter a valid full name (2–60 characters)."
    if not any(c.isalpha() for c in text):
        return None, "Please enter a valid full name — it should contain letters."

    letters_only = {c.lower() for c in text if c.isalpha()}
    if len(letters_only) <= 1 and len(text.replace(" ", "")) >= 4:
        return None, "That doesn't look like a real name. Please enter your actual full name."

    if text.lower() in _PLACEHOLDER_NAMES:
        return None, "Please enter your actual full name."

    if profanity.contains_profanity(text):
        return None, "That name isn't allowed. Please enter your actual full name."

    return text, None


def _validate_purpose(text: str):
    text = text.strip()
    if len(text) < 3 or len(text) > 200:
        return None, "Please enter a valid purpose (3–200 characters)."
    return text, None


def _validate_merchant(text: str):
    text = text.strip()
    if len(text) < 2 or len(text) > 80:
        return None, "Please enter a valid merchant name (2–80 characters)."
    if not any(c.isalpha() for c in text):
        return None, "Please enter a valid merchant name — it should contain letters."
    return text, None


def _validate_date(text: str):
    text = text.strip()
    if not re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2}", text):
        return None, "Please enter the date in MM/DD/YY format, e.g. 09/10/26."
    try:
        parsed = datetime.strptime(text, "%m/%d/%y")
    except ValueError:
        return None, "That's not a real calendar date. Please enter the date in MM/DD/YY format."
    if parsed.date() > datetime.now().date():
        return None, "That date is in the future. Please enter the actual purchase date (MM/DD/YY)."
    return parsed.strftime("%m/%d/%y"), None


def _validate_amount(text: str):
    text = text.strip().lstrip("$")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", text):
        return None, "Please enter a valid amount using just numbers, e.g. 24.99."
    value = float(text)
    if value <= 0:
        return None, "The amount must be greater than zero. Please enter the total amount."
    return f"{value:.2f}", None


# ---------- /start and /reimburse ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Hi! I'm the {config.CLUB_NAME} reimbursement bot.\n\n"
        "Use /reimburse to submit a receipt for reimbursement, /pay to "
        "submit a dues or trip payment, /edit to correct a mistake on a "
        "request that hasn't been decided yet, or /status to check your "
        "past submissions."
    )


async def reimburse_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["receipt_bytes"] = []  # list of raw image bytes, in-memory only

    user = update.effective_user
    full_name = user.full_name or "Unknown"
    telegram_name = f"{full_name} (@{user.username})" if user.username else full_name
    context.user_data["telegram_name"] = telegram_name

    await update.message.reply_text("Please enter your full name.")
    return ASK_NAME


async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name, error = _validate_name(update.message.text)
    if error:
        await update.message.reply_text(error)
        return ASK_NAME

    context.user_data["member_name"] = name
    await update.message.reply_text(
        "Please enter the purpose of this reimbursement (e.g. \"Food for fall outing\")."
    )
    return ASK_PURPOSE


async def ask_purpose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    purpose, error = _validate_purpose(update.message.text)
    if error:
        await update.message.reply_text(error)
        return ASK_PURPOSE

    context.user_data["purpose"] = purpose
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(cat, callback_data=f"cat_{cat}")]
         for cat in config.REIMBURSEMENT_CATEGORIES]
    )
    await update.message.reply_text("Please select a category.", reply_markup=keyboard)
    return ASK_CATEGORY


async def ask_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.replace("cat_", "", 1)
    context.user_data["category"] = category
    await query.edit_message_text(f"Category: {category}")
    await query.message.reply_text(
        "Now send a photo of your receipt. If you have more than one "
        "receipt, send them one at a time, then type \"done\" when finished."
    )
    return WAITING_PHOTO


# ---------- Photo handling (kept in memory, never written to disk) ----------

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    file = await photo.get_file()
    image_bytes = bytes(await file.download_as_bytearray())
    context.user_data["receipt_bytes"].append(image_bytes)

    await update.message.reply_text(
        f"Got receipt #{len(context.user_data['receipt_bytes'])}. "
        "Send another, or type \"done\" to continue."
    )
    return WAITING_PHOTO


async def handle_video_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "That's a video — please upload a photo of your receipt instead."
    )
    return WAITING_PHOTO


async def handle_unsupported_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Please upload a photo of your receipt (not a file, audio clip, or sticker)."
    )
    return WAITING_PHOTO


async def handle_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text != "done":
        await update.message.reply_text(
            "Send a receipt photo, or type \"done\" when you're finished uploading."
        )
        return WAITING_PHOTO

    receipts = context.user_data.get("receipt_bytes", [])
    if not receipts:
        await update.message.reply_text("You haven't sent any receipt photos yet.")
        return WAITING_PHOTO

    await update.message.reply_text("Reading the receipt(s)...")

    try:
        extracted = analyze_receipt(receipts[0])
        if len(receipts) > 1:
            extra_notes = extracted.get("notes") or ""
            extracted["notes"] = (
                extra_notes + f" ({len(receipts)} receipts submitted — "
                "verify combined total manually.)"
            ).strip()
    except Exception:
        logger.exception("Receipt analysis failed")
        await update.message.reply_text(
            "I had trouble reading that receipt automatically. Let's enter "
            "the details manually.\n\nPlease enter the merchant name."
        )
        context.user_data["manual"] = {}
        return MANUAL_FIX

    context.user_data["extracted"] = extracted

    if not extracted.get("is_receipt", True):
        context.user_data["receipt_bytes"] = []
        note = extracted.get("notes")
        message = "That doesn't look like a receipt — please upload a different photo."
        if note:
            message += f" ({note})"
        await update.message.reply_text(message)
        return WAITING_PHOTO

    vendor = extracted.get("vendor") or "unknown"
    date = extracted.get("date") or "unknown"
    amount = extracted.get("total_amount") or "unknown"
    confidence = extracted.get("confidence", "low")

    text = (
        f"Here's what I found (confidence: {confidence}):\n\n"
        f"Merchant: {vendor}\n"
        f"Date: {date}\n"
        f"Amount: ${amount}\n\n"
        "Please confirm the details above are correct."
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Yes", callback_data="confirm_yes"),
          InlineKeyboardButton("Edit", callback_data="confirm_no")]]
    )
    await update.message.reply_text(text, reply_markup=keyboard)
    return CONFIRM_DATA


async def confirm_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "confirm_yes":
        await query.edit_message_text("Great, submitting...")
        await finalize_submission(update, context, query.message)
        return ConversationHandler.END
    else:
        context.user_data["manual"] = {}
        await query.edit_message_text("Please enter the correct merchant name.")
        return MANUAL_FIX


async def manual_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    manual = context.user_data.setdefault("manual", {})
    text = update.message.text

    if "vendor" not in manual:
        vendor, error = _validate_merchant(text)
        if error:
            await update.message.reply_text(error)
            return MANUAL_FIX
        manual["vendor"] = vendor
        await update.message.reply_text("Please enter the purchase date (MM/DD/YY).")
        return MANUAL_FIX

    elif "date" not in manual:
        date, error = _validate_date(text)
        if error:
            await update.message.reply_text(error)
            return MANUAL_FIX
        manual["date"] = date
        await update.message.reply_text("Please enter the total amount (just the number, e.g. 24.99).")
        return MANUAL_FIX

    elif "total_amount" not in manual:
        amount, error = _validate_amount(text)
        if error:
            await update.message.reply_text(error)
            return MANUAL_FIX
        manual["total_amount"] = amount
        context.user_data["extracted"] = {
            "vendor": manual["vendor"],
            "date": manual["date"],
            "total_amount": manual["total_amount"],
            "tax": None,
            "items": [],
            "notes": "Entered manually by member.",
        }
        await update.message.reply_text("Submitting...")
        await finalize_submission(update, context, update.message)
        return ConversationHandler.END


# ---------- Write to Sheet, upload receipt, notify treasurer ----------

async def finalize_submission(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_target):
    extracted = context.user_data.get("extracted", {})
    receipts = context.user_data.get("receipt_bytes", [])

    submission_data = {
        "member_chat_id": update.effective_chat.id,
        "member_name": context.user_data.get("member_name"),
        "telegram_name": context.user_data.get("telegram_name", "Unknown"),
        "category": context.user_data.get("category"),
        "purpose": context.user_data.get("purpose"),
        "vendor": extracted.get("vendor"),
        "date": extracted.get("date"),
        "amount": extracted.get("total_amount"),
        "tax": extracted.get("tax"),
        "items": ", ".join(extracted.get("items", [])) if extracted.get("items") else "",
        "notes": extracted.get("notes") or "",
        "receipt_note": "Sent via Telegram" if receipts else "No receipt photo",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    try:
        code = sheets.append_submission(submission_data)
    except Exception:
        logger.exception("Could not write to Google Sheet")
        await reply_target.reply_text(
            "Something went wrong saving your submission. Please let the "
            "treasurer know directly for now — sorry about that!"
        )
        return

    await reply_target.reply_text(
        f"Submitted! Request {code} has been logged for the treasurer. "
        "I'll let you know once it's reviewed."
    )

    caption = (
        f"New Reimbursement Request\n"
        f"Member: {submission_data['member_name']}\n"
        f"Telegram: {submission_data['telegram_name']}\n"
        f"Merchant: {submission_data['vendor']}\n"
        f"Date: {submission_data['date']}\n"
        f"Amount: ${submission_data['amount']}\n"
        f"Category: {submission_data['category']}\n"
        f"Request ID: {code}"
    )

    try:
        if not receipts:
            await context.bot.send_message(chat_id=config.TREASURER_CHAT_ID, text=caption)
        elif len(receipts) == 1:
            await context.bot.send_photo(
                chat_id=config.TREASURER_CHAT_ID,
                photo=io.BytesIO(receipts[0]),
                caption=caption,
            )
        else:
            media = [InputMediaPhoto(io.BytesIO(receipts[0]), caption=caption)]
            media += [InputMediaPhoto(io.BytesIO(b)) for b in receipts[1:]]
            await context.bot.send_media_group(chat_id=config.TREASURER_CHAT_ID, media=media)
    except Exception:
        logger.exception("Could not notify treasurer")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------- /status ----------

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        ws = sheets.get_worksheet()
        records = ws.get_all_records(expected_headers=sheets.HEADERS)
    except Exception:
        logger.exception("Could not read Sheet for /status")
        await update.message.reply_text("Couldn't reach the Sheet right now, try again in a bit.")
        return

    chat_id_str = str(update.effective_chat.id)
    mine = [r for r in records if str(r.get("Member Chat ID")) == chat_id_str]
    if not mine:
        await update.message.reply_text("You have no reimbursement submissions yet.")
        return

    lines = ["Your submissions:"]
    for r in mine[-10:]:
        lines.append(f"{r['ID']} - {r['Merchant']} - ${r['Amount']} - {r['Status'].upper()}")
    await update.message.reply_text("\n".join(lines))


# ---------- Background job: notify members when Status changes ----------

async def check_for_decisions(context: ContextTypes.DEFAULT_TYPE):
    try:
        ws, pending = sheets.get_unnotified_decisions()
    except Exception:
        logger.exception("Could not check Sheet for decisions")
        return

    messages = {
        "approved": "Good news — your reimbursement request {code} for {vendor} (${amount}) was approved.",
        "denied": "Your reimbursement request {code} for {vendor} (${amount}) was not approved. Reach out to the treasurer if you have questions.",
        "needs more info": "Your reimbursement request {code} needs more information before it can be processed. The treasurer will follow up with you.",
    }

    for row_index, record in pending:
        status_key = str(record.get("Status", "")).strip().lower()
        text = messages.get(status_key)
        if not text:
            continue
        text = text.format(
            code=record.get("ID"), vendor=record.get("Merchant"), amount=record.get("Amount")
        )
        try:
            await context.bot.send_message(chat_id=int(record["Member Chat ID"]), text=text)
            sheets.mark_notified(ws, row_index, status_key)
        except Exception:
            logger.exception("Could not notify member for row %s", row_index)


async def check_stale_requests(context: ContextTypes.DEFAULT_TYPE):
    """Nudges the treasurer once per request when it's been Pending long
    enough to be eligible for a vote (matches the club's weekly meeting
    cadence), rather than flagging it as neglected."""
    try:
        stale = sheets.get_stale_pending_requests(config.STALE_REQUEST_DAYS)
    except Exception:
        logger.exception("Could not check Sheet for stale requests")
        return

    for record in stale:
        text = (
            f"Reminder: request {record.get('ID')} from {record.get('Member Name')} "
            f"for {record.get('Merchant')} (${record.get('Amount')}) has been pending "
            f"{config.STALE_REQUEST_DAYS}+ days and is now eligible for a vote."
        )
        try:
            await context.bot.send_message(chat_id=config.TREASURER_CHAT_ID, text=text)
            sheets.mark_reminded(record.get("ID"))
        except Exception:
            logger.exception("Could not send stale-request reminder for %s", record.get("ID"))


# ---------- /pay — dues and trip payments (separate flow, separate sheet tab) ----------

PAY_ASK_NAME, PAY_ASK_PURPOSE, PAY_ASK_TRIP_NAME, PAY_ASK_AMOUNT_BUTTONS, PAY_ASK_AMOUNT_TEXT, PAY_ASK_METHOD = range(300, 306)


async def pay_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    user = update.effective_user
    full_name = user.full_name or "Unknown"
    telegram_name = f"{full_name} (@{user.username})" if user.username else full_name
    context.user_data["pay_telegram_name"] = telegram_name

    await update.message.reply_text("Please enter your full name.")
    return PAY_ASK_NAME


async def pay_ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name, error = _validate_name(update.message.text)
    if error:
        await update.message.reply_text(error)
        return PAY_ASK_NAME

    context.user_data["pay_member_name"] = name
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(p, callback_data=f"payp_{p}")] for p in config.PAYMENT_PURPOSES]
    )
    await update.message.reply_text("Please select the purpose of this payment.", reply_markup=keyboard)
    return PAY_ASK_PURPOSE


async def pay_ask_purpose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    purpose = query.data.replace("payp_", "", 1)
    context.user_data["pay_purpose"] = purpose
    await query.edit_message_text(f"Purpose: {purpose}")

    if purpose == "Trip Payment":
        await query.message.reply_text(
            "Please enter which trip this payment is for (e.g. \"Fall Canoeing Trip\")."
        )
        return PAY_ASK_TRIP_NAME

    # Dues keeps the fixed $15/$20 buttons
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"${a}", callback_data=f"paya_{a}")] for a in config.PAYMENT_AMOUNTS]
    )
    await query.message.reply_text("Please select the amount paid.", reply_markup=keyboard)
    return PAY_ASK_AMOUNT_BUTTONS


async def pay_ask_trip_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trip_name, error = _validate_purpose(update.message.text)
    if error:
        await update.message.reply_text(error)
        return PAY_ASK_TRIP_NAME

    context.user_data["pay_trip_name"] = trip_name
    await update.message.reply_text("Please enter the amount paid, e.g. 24.99.")
    return PAY_ASK_AMOUNT_TEXT


async def pay_ask_amount_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount, error = _validate_amount(update.message.text)
    if error:
        await update.message.reply_text(error)
        return PAY_ASK_AMOUNT_TEXT

    context.user_data["pay_amount"] = amount
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(m, callback_data=f"paym_{m}")] for m in config.PAYMENT_METHODS]
    )
    await update.message.reply_text("Please select the payment method.", reply_markup=keyboard)
    return PAY_ASK_METHOD


async def pay_ask_amount_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    amount = query.data.replace("paya_", "", 1)
    context.user_data["pay_amount"] = amount

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(m, callback_data=f"paym_{m}")] for m in config.PAYMENT_METHODS]
    )
    await query.edit_message_text(f"Amount: ${amount}")
    await query.message.reply_text("Please select the payment method.", reply_markup=keyboard)
    return PAY_ASK_METHOD


async def pay_ask_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    method = query.data.replace("paym_", "", 1)
    context.user_data["pay_method"] = method
    await query.edit_message_text(f"Payment method: {method}")

    purpose = context.user_data.get("pay_purpose")
    trip_name = context.user_data.get("pay_trip_name")
    # Fold the trip name into Purpose (e.g. "Trip Payment - Fall Canoeing
    # Trip") instead of adding a new sheet column, matching the same
    # no-schema-disruption approach used elsewhere in this bot.
    display_purpose = f"{purpose} - {trip_name}" if trip_name else purpose

    payment_data = {
        "member_chat_id": update.effective_chat.id,
        "member_name": context.user_data.get("pay_member_name"),
        "telegram_name": context.user_data.get("pay_telegram_name", "Unknown"),
        "purpose": display_purpose,
        "amount": context.user_data.get("pay_amount"),
        "payment_method": method,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    try:
        code = sheets.append_payment(payment_data)
    except Exception:
        logger.exception("Could not write payment to Google Sheet")
        await query.message.reply_text(
            "Something went wrong saving this payment. Please let the "
            "treasurer know directly for now — sorry about that!"
        )
        return ConversationHandler.END

    await query.message.reply_text(
        f"Submitted! Payment {code} has been logged for the treasurer to confirm."
    )

    try:
        await context.bot.send_message(
            chat_id=config.TREASURER_CHAT_ID,
            text=(
                f"New Payment Submitted\n"
                f"Member: {payment_data['member_name']}\n"
                f"Telegram: {payment_data['telegram_name']}\n"
                f"Purpose: {payment_data['purpose']}\n"
                f"Amount: ${payment_data['amount']}\n"
                f"Method: {payment_data['payment_method']}\n"
                f"Request ID: {code}"
            ),
        )
    except Exception:
        logger.exception("Could not notify treasurer of payment")

    return ConversationHandler.END


async def check_for_payment_decisions(context: ContextTypes.DEFAULT_TYPE):
    try:
        ws, pending = sheets.get_unnotified_payment_decisions()
    except Exception:
        logger.exception("Could not check Sheet for payment decisions")
        return

    messages = {
        "approved": "Good news — your {purpose} payment of ${amount} ({code}) has been confirmed.",
        "denied": "Your {purpose} payment of ${amount} ({code}) could not be confirmed. Reach out to the treasurer if you have questions.",
        "needs more info": "Your {purpose} payment ({code}) needs more information. The treasurer will follow up with you.",
    }

    for row_index, record in pending:
        status_key = str(record.get("Status", "")).strip().lower()
        text = messages.get(status_key)
        if not text:
            continue
        text = text.format(
            code=record.get("ID"), purpose=record.get("Purpose"), amount=record.get("Amount")
        )
        try:
            await context.bot.send_message(chat_id=int(record["Member Chat ID"]), text=text)
            sheets.mark_payment_notified(ws, row_index, status_key)
        except Exception:
            logger.exception("Could not notify member for payment row %s", row_index)


# ---------- /edit — member fixes a mistake on their own still-pending request ----------

EDIT_CHOOSE_REQUEST, EDIT_CHOOSE_FIELD, EDIT_TEXT_VALUE, EDIT_CATEGORY_VALUE = range(200, 204)

_EDIT_FIELD_MAP = {
    "name": ("Member Name", _validate_name, "Please enter the corrected full name."),
    "purpose": ("Purpose", _validate_purpose, "Please enter the corrected purpose."),
    "merchant": ("Merchant", _validate_merchant, "Please enter the corrected merchant name."),
    "date": ("Purchase Date", _validate_date, "Please enter the corrected date (MM/DD/YY)."),
    "amount": ("Amount", _validate_amount, "Please enter the corrected amount, e.g. 24.99."),
}


async def edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    try:
        pending = sheets.get_member_pending_requests(update.effective_chat.id)
    except Exception:
        logger.exception("Could not read Sheet for /edit")
        await update.message.reply_text("Couldn't reach the Sheet right now — try again in a bit.")
        return ConversationHandler.END

    if not pending:
        await update.message.reply_text(
            "You have no pending requests to edit. Only requests the treasurer "
            "hasn't decided on yet can be changed."
        )
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(
            f"{r['ID']} - {r['Merchant']} (${r['Amount']})",
            callback_data=f"editreq_{row_index}",
        )]
        for row_index, r in pending
    ]
    await update.message.reply_text(
        "Please select which pending request to edit.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return EDIT_CHOOSE_REQUEST


async def edit_choose_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    row_index = int(query.data.replace("editreq_", "", 1))

    current_status = sheets.get_current_status(row_index).strip().lower()
    if current_status != "pending":
        await query.edit_message_text(
            "This request was just decided and can no longer be edited. "
            "Contact the treasurer directly if something needs correcting."
        )
        return ConversationHandler.END

    context.user_data["edit_row_index"] = row_index
    keyboard = [
        [InlineKeyboardButton("Name", callback_data="editfield_name"),
         InlineKeyboardButton("Purpose", callback_data="editfield_purpose")],
        [InlineKeyboardButton("Category", callback_data="editfield_category"),
         InlineKeyboardButton("Merchant", callback_data="editfield_merchant")],
        [InlineKeyboardButton("Date", callback_data="editfield_date"),
         InlineKeyboardButton("Amount", callback_data="editfield_amount")],
    ]
    await query.edit_message_text(
        "Please select which field to correct.", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return EDIT_CHOOSE_FIELD


async def edit_choose_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    field_key = query.data.replace("editfield_", "", 1)

    if field_key == "category":
        keyboard = [
            [InlineKeyboardButton(cat, callback_data=f"editcatval_{cat}")]
            for cat in config.REIMBURSEMENT_CATEGORIES
        ]
        await query.edit_message_text(
            "Please select the corrected category.", reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return EDIT_CATEGORY_VALUE

    header, _validator, prompt = _EDIT_FIELD_MAP[field_key]
    context.user_data["edit_field_key"] = field_key
    await query.edit_message_text(prompt)
    return EDIT_TEXT_VALUE


async def edit_text_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field_key = context.user_data["edit_field_key"]
    header, validator, prompt = _EDIT_FIELD_MAP[field_key]

    value, error = validator(update.message.text)
    if error:
        await update.message.reply_text(error)
        return EDIT_TEXT_VALUE

    row_index = context.user_data["edit_row_index"]
    current_status = sheets.get_current_status(row_index).strip().lower()
    if current_status != "pending":
        await update.message.reply_text(
            "This request was just decided and can no longer be edited. "
            "Contact the treasurer directly if something needs correcting."
        )
        return ConversationHandler.END

    try:
        sheets.update_field(row_index, header, value)
    except Exception:
        logger.exception("Could not apply edit to row %s", row_index)
        await update.message.reply_text("Something went wrong saving that change — please try again.")
        return ConversationHandler.END

    await update.message.reply_text(f"Updated. {header} is now: {value}")
    return ConversationHandler.END


async def edit_category_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.replace("editcatval_", "", 1)

    row_index = context.user_data["edit_row_index"]
    current_status = sheets.get_current_status(row_index).strip().lower()
    if current_status != "pending":
        await query.edit_message_text(
            "This request was just decided and can no longer be edited. "
            "Contact the treasurer directly if something needs correcting."
        )
        return ConversationHandler.END

    try:
        sheets.update_field(row_index, "Category", category)
    except Exception:
        logger.exception("Could not apply category edit to row %s", row_index)
        await query.edit_message_text("Something went wrong saving that change — please try again.")
        return ConversationHandler.END

    await query.edit_message_text(f"Updated. Category is now: {category}")
    return ConversationHandler.END


async def switch_command_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lets one command cleanly interrupt another still-active conversation.
    Without this, e.g. typing /pay while /reimburse is mid-flow (stuck on
    an unanswered question) gets silently swallowed by the reimburse
    conversation instead of starting the payment flow."""
    context.user_data.clear()
    command = update.message.text.strip().split()[0]
    await update.message.reply_text(
        f"Cancelled your incomplete request. Please send {command} again to start it."
    )
    return ConversationHandler.END


def main():
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("reimburse", reimburse_start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
            ASK_PURPOSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_purpose)],
            ASK_CATEGORY: [CallbackQueryHandler(ask_category, pattern="^cat_")],
            WAITING_PHOTO: [
                MessageHandler(filters.PHOTO, handle_photo),
                MessageHandler(filters.VIDEO | filters.ANIMATION, handle_video_upload),
                MessageHandler(
                    filters.Document.ALL | filters.AUDIO | filters.VOICE | filters.Sticker.ALL,
                    handle_unsupported_upload,
                ),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_done),
            ],
            CONFIRM_DATA: [CallbackQueryHandler(confirm_data, pattern="^confirm_")],
            MANUAL_FIX: [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_fix)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("pay", switch_command_fallback),
            CommandHandler("edit", switch_command_fallback),
        ],
        conversation_timeout=300,
    )

    pay_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("pay", pay_start)],
        states={
            PAY_ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_ask_name)],
            PAY_ASK_PURPOSE: [CallbackQueryHandler(pay_ask_purpose, pattern="^payp_")],
            PAY_ASK_TRIP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_ask_trip_name)],
            PAY_ASK_AMOUNT_BUTTONS: [CallbackQueryHandler(pay_ask_amount_buttons, pattern="^paya_")],
            PAY_ASK_AMOUNT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_ask_amount_text)],
            PAY_ASK_METHOD: [CallbackQueryHandler(pay_ask_method, pattern="^paym_")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("reimburse", switch_command_fallback),
            CommandHandler("edit", switch_command_fallback),
        ],
        conversation_timeout=300,
    )

    edit_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("edit", edit_start)],
        states={
            EDIT_CHOOSE_REQUEST: [CallbackQueryHandler(edit_choose_request, pattern="^editreq_")],
            EDIT_CHOOSE_FIELD: [CallbackQueryHandler(edit_choose_field, pattern="^editfield_")],
            EDIT_TEXT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_text_value)],
            EDIT_CATEGORY_VALUE: [CallbackQueryHandler(edit_category_value, pattern="^editcatval_")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("reimburse", switch_command_fallback),
            CommandHandler("pay", switch_command_fallback),
        ],
        conversation_timeout=300,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(conv_handler)
    app.add_handler(pay_conv_handler)
    app.add_handler(edit_conv_handler)

    app.job_queue.run_repeating(
        check_for_payment_decisions,
        interval=config.STATUS_CHECK_INTERVAL_MINUTES * 60,
        first=45,
    )

    app.job_queue.run_repeating(
        check_for_decisions,
        interval=config.STATUS_CHECK_INTERVAL_MINUTES * 60,
        first=30,
    )
    app.job_queue.run_repeating(
        check_stale_requests,
        interval=24 * 60 * 60,  # once daily is plenty for a 7-day threshold
        first=60,
    )

    if config.RUN_MODE == "webhook":
        logger.info("Starting in webhook mode on port %s", config.PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=config.PORT,
            url_path=config.TELEGRAM_BOT_TOKEN,
            webhook_url=f"{config.WEBHOOK_URL}/{config.TELEGRAM_BOT_TOKEN}",
        )
    else:
        logger.info("Starting in polling mode")
        app.run_polling()


if __name__ == "__main__":
    main()

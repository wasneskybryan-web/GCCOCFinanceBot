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
from datetime import datetime

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


# ---------- /start and /reimburse ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Hi! I'm the {config.CLUB_NAME} reimbursement bot.\n\n"
        "Use /reimburse to submit a receipt for reimbursement, or /status to "
        "check your past submissions."
    )


async def reimburse_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["receipt_bytes"] = []  # list of raw image bytes, in-memory only

    user = update.effective_user
    full_name = user.full_name or "Unknown"
    telegram_name = f"{full_name} (@{user.username})" if user.username else full_name
    context.user_data["telegram_name"] = telegram_name

    await update.message.reply_text("What's your full name?")
    return ASK_NAME


async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["member_name"] = update.message.text.strip()
    await update.message.reply_text(
        "What's this reimbursement for? (e.g. \"Food for fall outing\")"
    )
    return ASK_PURPOSE


async def ask_purpose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["purpose"] = update.message.text.strip()
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(cat, callback_data=f"cat_{cat}")]
         for cat in config.REIMBURSEMENT_CATEGORIES]
    )
    await update.message.reply_text("Pick a category:", reply_markup=keyboard)
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
            "the details manually.\n\nWhat's the merchant name?"
        )
        context.user_data["manual"] = {}
        return MANUAL_FIX

    context.user_data["extracted"] = extracted

    vendor = extracted.get("vendor") or "unknown"
    date = extracted.get("date") or "unknown"
    amount = extracted.get("total_amount") or "unknown"
    confidence = extracted.get("confidence", "low")

    text = (
        f"Here's what I found (confidence: {confidence}):\n\n"
        f"Merchant: {vendor}\n"
        f"Date: {date}\n"
        f"Amount: ${amount}\n\n"
        "Is this correct?"
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
        await query.edit_message_text("What's the correct merchant name?")
        return MANUAL_FIX


async def manual_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    manual = context.user_data.setdefault("manual", {})
    text = update.message.text.strip()

    if "vendor" not in manual:
        manual["vendor"] = text
        await update.message.reply_text("What's the purchase date? (MM/DD/YY)")
        return MANUAL_FIX
    elif "date" not in manual:
        manual["date"] = text
        await update.message.reply_text("What's the total amount? (just the number, e.g. 24.99)")
        return MANUAL_FIX
    elif "total_amount" not in manual:
        manual["total_amount"] = text
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
            sheets.mark_notified(ws, row_index)
        except Exception:
            logger.exception("Could not notify member for row %s", row_index)


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
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_done),
            ],
            CONFIRM_DATA: [CallbackQueryHandler(confirm_data, pattern="^confirm_")],
            MANUAL_FIX: [MessageHandler(filters.TEXT & ~filters.COMMAND, manual_fix)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(conv_handler)

    app.job_queue.run_repeating(
        check_for_decisions,
        interval=config.STATUS_CHECK_INTERVAL_MINUTES * 60,
        first=30,
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

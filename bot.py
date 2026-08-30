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
import random
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
    ApplicationHandlerStop,
    filters,
)

import config
import sheets
from receipt_analyzer import analyze_receipt

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

ASK_NAME, ASK_PURPOSE, ASK_CATEGORY, WAITING_PHOTO, CONFIRM_DATA, MANUAL_FIX, CONFIRM_EDIT_FIELD, CONFIRM_EDIT_VALUE = range(8)


# ---------- Group chats are off-limits — silence for everyone except one
# specific person, who gets told off instead. This runs before every other
# handler (registered in group=-1) and stops all further processing. ----------

_ROAST_MESSAGES = [
    "I'm going to need you to stop contributing to the conversation.",
    "Respectfully, please return to silence.",
    "You've had enough speaking privileges for today.",
    "That's fascinating. Now shut the fuck up.",
    "I say this with the utmost respect: nobody asked.",
    "Your microphone privileges have been revoked.",
    "Please enjoy the exciting new experience of not talking.",
    "I'm gonna need you to put those thoughts back where you found them.",
    "You make a compelling argument for mandatory silence.",
    "Brother, I love you dearly, but I am begging you to become quiet.",
]

# King James Version, Genesis chapter 1 — public domain. Long enough to
# exceed Telegram's 4096-character message limit, so it gets sent in
# verse-aligned chunks rather than as one giant message.
_GENESIS_1_VERSES = [
    "1 In the beginning God created the heaven and the earth.",
    "2 And the earth was without form, and void; and darkness was upon the face of the deep. And the Spirit of God moved upon the face of the waters.",
    "3 And God said, Let there be light: and there was light.",
    "4 And God saw the light, that it was good: and God divided the light from the darkness.",
    "5 And God called the light Day, and the darkness he called Night. And the evening and the morning were the first day.",
    "6 And God said, Let there be a firmament in the midst of the waters, and let it divide the waters from the waters.",
    "7 And God made the firmament, and divided the waters which were under the firmament from the waters which were above the firmament: and it was so.",
    "8 And God called the firmament Heaven. And the evening and the morning were the second day.",
    "9 And God said, Let the waters under the heaven be gathered together unto one place, and let the dry land appear: and it was so.",
    "10 And God called the dry land Earth; and the gathering together of the waters called he Seas: and God saw that it was good.",
    "11 And God said, Let the earth bring forth grass, the herb yielding seed, and the fruit tree yielding fruit after his kind, whose seed is in itself, upon the earth: and it was so.",
    "12 And the earth brought forth grass, and herb yielding seed after his kind, and the tree yielding fruit, whose seed was in itself, after his kind: and God saw that it was good.",
    "13 And the evening and the morning were the third day.",
    "14 And God said, Let there be lights in the firmament of the heaven to divide the day from the night; and let them be for signs, and for seasons, and for days, and years:",
    "15 And let them be for lights in the firmament of the heaven to give light upon the earth: and it was so.",
    "16 And God made two great lights; the greater light to rule the day, and the lesser light to rule the night: he made the stars also.",
    "17 And God set them in the firmament of the heaven to give light upon the earth,",
    "18 And to rule over the day and over the night, and to divide the light from the darkness: and God saw that it was good.",
    "19 And the evening and the morning were the fourth day.",
    "20 And God said, Let the waters bring forth abundantly the moving creature that hath life, and fowl that may fly above the earth in the open firmament of heaven.",
    "21 And God created great whales, and every living creature that moveth, which the waters brought forth abundantly, after their kind, and every winged fowl after his kind: and God saw that it was good.",
    "22 And God blessed them, saying, Be fruitful, and multiply, and fill the waters in the seas, and let fowl multiply in the earth.",
    "23 And the evening and the morning were the fifth day.",
    "24 And God said, Let the earth bring forth the living creature after his kind, cattle, and creeping thing, and beast of the earth after his kind: and it was so.",
    "25 And God made the beast of the earth after his kind, and cattle after their kind, and every thing that creepeth upon the earth after his kind: and God saw that it was good.",
    "26 And God said, Let us make man in our image, after our likeness: and let them have dominion over the fish of the sea, and over the fowl of the air, and over the cattle, and over all the earth, and over every creeping thing that creepeth upon the earth.",
    "27 So God created man in his own image, in the image of God created he him; male and female created he them.",
    "28 And God blessed them, and God said unto them, Be fruitful, and multiply, and replenish the earth, and subdue it: and have dominion over the fish of the sea, and over the fowl of the air, and over every living thing that moveth upon the earth.",
    "29 And God said, Behold, I have given you every herb bearing seed, which is upon the face of the whole earth, and every tree, in the which is the fruit of a tree yielding seed; to you it shall be for meat.",
    "30 And to every beast of the earth, and to every fowl of the air, and to every thing that creepeth upon the earth, wherein there is life, I have given every green herb for meat: and it was so.",
    "31 And God saw every thing that he had made, and, behold, it was very good. And the evening and the morning were the sixth day.",
]


def _chunk_verses(verses, limit=3500):
    """Groups verses into chunks that stay under Telegram's message limit,
    breaking only between verses so nothing gets cut mid-sentence."""
    chunks, current = [], ""
    for verse in verses:
        candidate = f"{current}\n{verse}" if current else verse
        if len(candidate) > limit:
            chunks.append(current)
            current = verse
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


_GENESIS_1_CHUNKS = _chunk_verses(_GENESIS_1_VERSES)


def _is_the_buddy(user) -> bool:
    if not config.BUDDY_USERNAME or not user or not user.username:
        return False
    return user.username.strip().lower().lstrip("@") == config.BUDDY_USERNAME


async def _send_roast(bot, chat_id):
    """Picks a random response — usually a short quip, occasionally the
    entire first chapter of Genesis instead, sent as multiple messages."""
    pool = _ROAST_MESSAGES + ["GENESIS"]
    choice = random.choice(pool)
    try:
        if choice == "GENESIS":
            for chunk in _GENESIS_1_CHUNKS:
                await bot.send_message(chat_id=chat_id, text=chunk)
        else:
            await bot.send_message(chat_id=chat_id, text=choice)
    except Exception:
        pass


def _message_targets_bot(update: Update, bot_username: str) -> bool:
    """True if this message is a command, a reply to one of the bot's own
    messages, or @mentions the bot — as opposed to just any message in the
    group that happens to arrive now that Privacy Mode is off."""
    message = update.effective_message
    if not message or not message.text:
        return False
    text = message.text.strip()

    if text.startswith("/"):
        return True

    reply_to = message.reply_to_message
    if reply_to and reply_to.from_user and reply_to.from_user.is_bot:
        return True

    if bot_username and f"@{bot_username.lower()}" in text.lower():
        return True

    return False


async def block_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_the_buddy(update.effective_user) and _message_targets_bot(update, context.bot.username):
        await _send_roast(context.bot, update.effective_chat.id)
    raise ApplicationHandlerStop


async def block_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        return  # not a group — let normal button handlers process it

    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    if _is_the_buddy(update.effective_user):
        await _send_roast(context.bot, update.effective_chat.id)
    raise ApplicationHandlerStop

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

def _telegram_identity(user) -> str:
    full_name = user.full_name or "Unknown"
    return f"{full_name} (@{user.username})" if user.username else full_name


async def _track_member(update: Update):
    """Records this chat ID in the hidden member list (used by /broadcast)
    the first time we ever see it. Failures here are non-fatal — never
    block the actual command over this."""
    try:
        sheets.record_known_member(update.effective_chat.id, _telegram_identity(update.effective_user))
    except Exception:
        logger.exception("Could not record known member")


async def _reply(update: Update, text: str, **kwargs):
    """Sends a message whether this update came from a typed command or a
    button tap — lets the same function serve both /reimburse and the
    'Submit a Reimbursement' button, for example."""
    if update.message:
        await update.message.reply_text(text, **kwargs)
    else:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, **kwargs)


def _action_menu_keyboard(include_done: bool = False):
    rows = [
        [InlineKeyboardButton("🧾 Submit a Reimbursement", callback_data="postaction_reimburse")],
        [InlineKeyboardButton("💳 Submit Dues/Trip Payment", callback_data="postaction_pay")],
        [InlineKeyboardButton("✏️ Edit a Pending Request", callback_data="postaction_edit")],
        [InlineKeyboardButton("📋 Check My Status", callback_data="postaction_status")],
    ]
    if include_done:
        rows.append([InlineKeyboardButton("Nothing else, thanks!", callback_data="postaction_done")])
    return InlineKeyboardMarkup(rows)


async def send_action_menu(reply_target, prompt: str = "Would you like to do anything else?"):
    await reply_target.reply_text(prompt, reply_markup=_action_menu_keyboard(include_done=True))


async def postaction_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Okay! Let me know if you need anything else. 🙂")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _track_member(update)
    await _reply(
        update,
        f"Hi! I'm the {config.CLUB_NAME} reimbursement bot.\n\n"
        "Choose an option below, or type /help to see all commands.",
        reply_markup=_action_menu_keyboard(),
    )


async def reimburse_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["receipt_bytes"] = []  # list of raw image bytes, in-memory only
    await _track_member(update)

    context.user_data["telegram_name"] = _telegram_identity(update.effective_user)

    await _reply(
        update,
        "Please enter your full name.\n\n"
        "⚠️ Please make sure everything you enter in this process is "
        "accurate — you're responsible for the information you submit.",
    )
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


def _confirm_summary_text(extracted: dict) -> str:
    vendor = extracted.get("vendor") or "unknown"
    date = extracted.get("date") or "unknown"
    amount = extracted.get("total_amount") or "unknown"
    confidence = extracted.get("confidence", "low")
    return (
        f"Here's what I found (confidence: {confidence}):\n\n"
        f"Merchant: {vendor}\n"
        f"Date: {date}\n"
        f"Amount: ${amount}\n\n"
        "Please confirm the details above are correct.\n\n"
        "⚠️ You're responsible for the accuracy of this information before confirming."
    )


def _confirm_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Yes", callback_data="confirm_yes"),
          InlineKeyboardButton("Edit", callback_data="confirm_no")]]
    )


_CONFIRM_EDIT_FIELD_MAP = {
    "merchant": ("vendor", _validate_merchant, "Please enter the corrected merchant name."),
    "date": ("date", _validate_date, "Please enter the corrected date (MM/DD/YY)."),
    "amount": ("total_amount", _validate_amount, "Please enter the corrected amount, e.g. 24.99."),
}


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

    await update.message.reply_text(
        _confirm_summary_text(extracted), reply_markup=_confirm_keyboard()
    )
    return CONFIRM_DATA


async def confirm_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "confirm_yes":
        await query.edit_message_text("Great, submitting...")
        await finalize_submission(update, context, query.message)
        return ConversationHandler.END

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Merchant", callback_data="confedit_merchant")],
            [InlineKeyboardButton("Date", callback_data="confedit_date")],
            [InlineKeyboardButton("Amount", callback_data="confedit_amount")],
        ]
    )
    await query.edit_message_text(
        "Please select which field is incorrect.", reply_markup=keyboard
    )
    return CONFIRM_EDIT_FIELD


async def confirm_edit_choose_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    field_key = query.data.replace("confedit_", "", 1)
    internal_key, validator, prompt = _CONFIRM_EDIT_FIELD_MAP[field_key]
    context.user_data["confirm_edit_field"] = field_key
    await query.edit_message_text(prompt)
    return CONFIRM_EDIT_VALUE


async def confirm_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field_key = context.user_data["confirm_edit_field"]
    internal_key, validator, _prompt = _CONFIRM_EDIT_FIELD_MAP[field_key]

    value, error = validator(update.message.text)
    if error:
        await update.message.reply_text(error)
        return CONFIRM_EDIT_VALUE

    context.user_data["extracted"][internal_key] = value
    await update.message.reply_text(
        _confirm_summary_text(context.user_data["extracted"]), reply_markup=_confirm_keyboard()
    )
    return CONFIRM_DATA


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

    await send_action_menu(reply_target)


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
        await _reply(update, "Couldn't reach the Sheet right now, try again in a bit.")
        return

    chat_id_str = str(update.effective_chat.id)
    mine = [r for r in records if str(r.get("Member Chat ID")) == chat_id_str]
    if not mine:
        await _reply(update, "You have no reimbursement submissions yet.")
        return

    lines = ["Your submissions:"]
    for r in mine[-10:]:
        lines.append(f"{r['ID']} - {r['Merchant']} - ${r['Amount']} - {r['Status'].upper()}")
    await _reply(update, "\n".join(lines))


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
    await _track_member(update)
    context.user_data["pay_telegram_name"] = _telegram_identity(update.effective_user)

    await _reply(
        update,
        "Please enter your full name.\n\n"
        "⚠️ Please make sure everything you enter in this process is "
        "accurate — you're responsible for the information you submit.",
    )
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

    await send_action_menu(query.message)
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
        await _reply(update, "Couldn't reach the Sheet right now — try again in a bit.")
        return ConversationHandler.END

    if not pending:
        await _reply(
            update,
            "You have no pending requests to edit. Only requests the treasurer "
            "hasn't decided on yet can be changed.",
        )
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(
            f"{r['ID']} - {r['Merchant']} (${r['Amount']})",
            callback_data=f"editreq_{row_index}",
        )]
        for row_index, r in pending
    ]
    await _reply(
        update,
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
    await send_action_menu(update.message)
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
    await send_action_menu(query.message)
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


# ---------- Admin commands: /approve, /deny, /broadcast ----------

def _is_treasurer(user_id) -> bool:
    return str(user_id) == str(config.TREASURER_CHAT_ID)


_DECISION_MESSAGES = {
    "reimbursement": {
        "approved": "Good news — your reimbursement request {code} for {label} (${amount}) was approved.",
        "denied": "Your reimbursement request {code} for {label} (${amount}) was not approved. Reach out to the treasurer if you have questions.",
    },
    "payment": {
        "approved": "Good news — your {label} payment of ${amount} ({code}) has been confirmed.",
        "denied": "Your {label} payment of ${amount} ({code}) could not be confirmed. Reach out to the treasurer if you have questions.",
    },
}


async def _decide_command(update: Update, context: ContextTypes.DEFAULT_TYPE, status: str):
    if not _is_treasurer(update.effective_user.id):
        await update.message.reply_text("Only the treasurer can use this command.")
        return

    if not context.args:
        await update.message.reply_text(f"Usage: /{status.lower()} GCCOC-14 (the request ID)")
        return

    code = context.args[0]
    try:
        result = sheets.find_request_by_code(code)
    except Exception:
        logger.exception("Could not search sheets for %s", code)
        await update.message.reply_text("Couldn't reach the Sheet right now — try again in a bit.")
        return

    if not result:
        await update.message.reply_text(f"No request found with ID {code}.")
        return

    sheet_type, ws, row_index, record = result

    try:
        sheets.set_status(sheet_type, ws, row_index, status)
        if sheet_type == "reimbursement":
            sheets.mark_notified(ws, row_index, status)
        else:
            sheets.mark_payment_notified(ws, row_index, status)
    except Exception:
        logger.exception("Could not update status for %s", code)
        await update.message.reply_text("Something went wrong updating that request — try again.")
        return

    label = record.get("Merchant") if sheet_type == "reimbursement" else record.get("Purpose")
    text = _DECISION_MESSAGES[sheet_type][status.lower()].format(
        code=record.get("ID"), label=label, amount=record.get("Amount")
    )
    try:
        await context.bot.send_message(chat_id=int(record["Member Chat ID"]), text=text)
    except Exception:
        logger.exception("Could not notify member for %s", code)

    await update.message.reply_text(f"{record.get('ID')} marked {status}. Member notified.")


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _decide_command(update, context, "Approved")


async def deny_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _decide_command(update, context, "Denied")


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_treasurer(update.effective_user.id):
        await update.message.reply_text("Only the treasurer can use this command.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /broadcast Your message here")
        return

    message = " ".join(context.args)
    try:
        chat_ids = sheets.get_all_known_member_chat_ids()
    except Exception:
        logger.exception("Could not read known members for broadcast")
        await update.message.reply_text("Couldn't reach the Sheet right now — try again in a bit.")
        return

    if not chat_ids:
        await update.message.reply_text(
            "No known members to message yet — this list fills in as people use /start."
        )
        return

    sent, failed = 0, 0
    for chat_id in chat_ids:
        try:
            await context.bot.send_message(chat_id=int(chat_id), text=f"📢 {message}")
            sent += 1
        except Exception:
            failed += 1

    summary = f"Broadcast sent to {sent} member(s)."
    if failed:
        summary += f" {failed} failed to deliver (they may have blocked the bot)."
    await update.message.reply_text(summary)


_MEMBER_HELP_TEXT = (
    "Available commands:\n\n"
    "/reimburse - Submit a receipt for reimbursement\n"
    "/pay - Submit a dues or trip payment\n"
    "/edit - Correct a mistake on a request that hasn't been decided yet\n"
    "/status - Check your past submissions\n"
    "/cancel - Cancel whatever you're currently doing\n"
    "/help - Show this list"
)

_ADMIN_HELP_TEXT = _MEMBER_HELP_TEXT + (
    "\n\nAdmin commands:\n"
    "/approve <code> - Approve a request and notify the member\n"
    "/deny <code> - Deny a request and notify the member\n"
    "/broadcast <message> - Message every known member at once"
)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_treasurer(update.effective_user.id):
        await _reply(update, _ADMIN_HELP_TEXT)
    else:
        await _reply(update, _MEMBER_HELP_TEXT)


# Hidden easter egg — deliberately not listed in /help or the /start
# button menu. Sends an existing public-domain photo, not anything
# generated. Only works in private chats (group chats are blocked
# entirely by block_group_message before this could ever run).
_OBAMA_PHOTO_URL = "https://commons.wikimedia.org/wiki/Special:FilePath/Official_portrait_of_Barack_Obama.jpg"


async def obama_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=_OBAMA_PHOTO_URL)
    except Exception:
        logger.exception("Could not send Obama photo")


def main():
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Runs before everything else — blocks all group-chat activity except
    # the one roasted user, regardless of what command/button was used.
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, block_group_message), group=-1)
    app.add_handler(CallbackQueryHandler(block_group_callback), group=-1)

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("reimburse", reimburse_start),
            CallbackQueryHandler(reimburse_start, pattern="^postaction_reimburse$"),
        ],
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
            CONFIRM_EDIT_FIELD: [CallbackQueryHandler(confirm_edit_choose_field, pattern="^confedit_")],
            CONFIRM_EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_edit_value)],
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
        entry_points=[
            CommandHandler("pay", pay_start),
            CallbackQueryHandler(pay_start, pattern="^postaction_pay$"),
        ],
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
        entry_points=[
            CommandHandler("edit", edit_start),
            CallbackQueryHandler(edit_start, pattern="^postaction_edit$"),
        ],
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
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("deny", deny_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("obama", obama_command))
    app.add_handler(CallbackQueryHandler(status, pattern="^postaction_status$"))
    app.add_handler(CallbackQueryHandler(postaction_done, pattern="^postaction_done$"))
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

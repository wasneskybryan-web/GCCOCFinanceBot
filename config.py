"""
Central config. All secrets come from environment variables.
"""
import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Optional. Free from https://aistudio.google.com/apikey — no credit card
# required, ever. If set, receipt reading uses Gemini (much more accurate).
# If left blank, the bot automatically falls back to free local OCR instead.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Your personal Telegram numeric user ID (the treasurer).
# New submissions get sent here as a plain text notification.
TREASURER_CHAT_ID = os.environ.get("TREASURER_CHAT_ID")

# Optional. A Telegram @username (with or without the @) of a specific
# person who gets a snarky canned reply if they try to use the bot in a
# group chat. Matched directly from their username on each message, so
# there's no need to ask them for their numeric ID.
BUDDY_USERNAME = os.environ.get("BUDDY_USERNAME", "").strip().lower().lstrip("@")

# Paste the ENTIRE contents of your Google service account JSON key file
# here as one environment variable value (see README for how to get this).
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

# The ID from your Google Sheet's URL:
# https://docs.google.com/spreadsheets/d/THIS_PART_HERE/edit
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")

# How often (in minutes) the bot checks the Sheet for Status changes so it
# can notify members automatically.
STATUS_CHECK_INTERVAL_MINUTES = int(os.environ.get("STATUS_CHECK_INTERVAL_MINUTES", "5"))

# A Pending request older than this many days gets you a one-time reminder
# (matches a weekly vote cadence — 7 days means it's now eligible to be
# decided at the next meeting, not that it was forgotten).
STALE_REQUEST_DAYS = int(os.environ.get("STALE_REQUEST_DAYS", "7"))

# --- Branding ---
CLUB_NAME = os.environ.get("CLUB_NAME", "Grove City College Outing Club")
CLUB_ABBREVIATION = os.environ.get("CLUB_ABBREVIATION", "GCCOC")

REIMBURSEMENT_CATEGORIES = [
    "Club Supplies",
    "Food/Refreshments",
    "Equipment",
    "Travel/Transportation",
    "Event Fees",
    "Other",
]

PAYMENT_PURPOSES = ["Dues", "Trip Payment"]
PAYMENT_AMOUNTS = ["15", "20"]
PAYMENT_METHODS = ["Cash", "Venmo"]

# Set to "webhook" when deployed to a cloud host with a public HTTPS URL
# (e.g. Railway), or "polling" for local/dev use.
RUN_MODE = os.environ.get("RUN_MODE", "polling")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # e.g. https://yourapp.up.railway.app
PORT = int(os.environ.get("PORT", "8080"))

REQUIRED = {
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "TREASURER_CHAT_ID": TREASURER_CHAT_ID,
    "GOOGLE_SERVICE_ACCOUNT_JSON": GOOGLE_SERVICE_ACCOUNT_JSON,
    "GOOGLE_SHEET_ID": GOOGLE_SHEET_ID,
}

missing = [k for k, v in REQUIRED.items() if not v]
if missing:
    raise RuntimeError(
        f"Missing required environment variables: {', '.join(missing)}. "
        f"Copy .env.example to .env and fill these in."
    )

if RUN_MODE == "webhook" and not WEBHOOK_URL:
    raise RuntimeError("RUN_MODE=webhook requires WEBHOOK_URL to be set.")

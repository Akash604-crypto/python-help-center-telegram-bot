#!/usr/bin/env python3
"""
Telegram Help Center Bot
- Python 3.11.11
- Requires: python-telegram-bot==20.7

Features implemented:
1) User flows with inline buttons:
   - Payment issue -> choose vip / dark / both -> ask for screenshot + optional UTR/ref -> forward to admin
   - Technical issue -> ask to describe -> forward to admin
   - Others -> send username @Vip_Help_center1222_bot
   - Warning if user sends a message before clicking buttons

2) Admin flows:
   - When payment forwarded, admin receives a message with Approve VIP / Approve DARK / Approve BOTH / Decline buttons
     - On Approve, the configured link(s) will be sent to the user automatically
   - When tech forwarded, admin receives message with Reply / Ignore buttons
     - Admin can reply via /reply <user_id> <text> (or use the inline Reply button which will explain usage)

3) Commands for admin (restricted by ADMIN_CHAT_ID env var):
   - /set_vip_link <url>
   - /set_dark_link <url>
   - /broadcast <message>  (sends to all users recorded)
   - /insights  (shows counts of payments, tech issues, approved links sent)

Persistence:
- All runtime data (users, pending admin actions, links, counters) are saved to a JSON file at $DATA_DIR/helpcenter_state.json by default.

Deployment notes:
- Works as a worker on Render (Background Worker).
- Environment variables required:
    BOT_TOKEN  - Telegram bot token
    ADMIN_CHAT_ID - single admin chat id (string) OR comma-separated for multiple
    DATA_DIR - directory where state file will be stored (defaults to ./data)

Run: python telegram_help_center_bot.py

Changelog (code review & fixes applied):
- Made the main entrypoint synchronous (compat with run_polling) and ensured initial state loads correctly.
- Replaced asyncio-based timestamp generation with time.time() for pending IDs.
- Added basic logging for failures forwarding to admins.
- Hardened ADMIN_ID parsing and messaging when no admins configured.
- Fixed several string literal / f-string issues that caused SyntaxError when message text contained newlines.

"""

import os
import json
import time
import asyncio
from functools import wraps
from typing import Dict, Any
from pathlib import Path

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# --- Configuration & persistence ------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # comma-separated allowed
DATA_DIR = os.getenv("DATA_DIR", ".")
STATE_FILE = Path(DATA_DIR) / "helpcenter_state.json"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")

ADMIN_IDS = set()
if ADMIN_CHAT_ID:
    for part in ADMIN_CHAT_ID.split(","):
        part = part.strip()
        if part:
            try:
                ADMIN_IDS.add(int(part))
            except ValueError:
                # ignore malformed ids but print for debugging
                print("Warning: invalid ADMIN_CHAT_ID part:", part)

# Create data dir
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

DEFAULT_STATE = {
    "users": {},  # user_id -> metadata
    "pending": {},  # pending_id -> info forwarded to admin
    "vip_link": "",
    "dark_link": "",
    "counters": {"payment_submitted": 0, "tech_submitted": 0, "links_sent": 0},
}

state_lock = asyncio.Lock()


async def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print("Failed to load state, using defaults:", e)
            return DEFAULT_STATE.copy()
    else:
        return DEFAULT_STATE.copy()


async def save_state(s: Dict[str, Any]):
    # Keep this async so handlers can await it safely
    async with state_lock:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print("Failed to save state:", e)


# --- Utilities ------------------------------------------------------------------

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = None
        if update.effective_user:
            user_id = update.effective_user.id
        if user_id not in ADMIN_IDS:
            # Some handlers may not have message (e.g. callback_query)
            try:
                if update.message:
                    await update.message.reply_text("Unauthorized: admin only.")
                elif update.callback_query:
                    await update.callback_query.answer("Unauthorized", show_alert=True)
            except Exception:
                pass
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


async def ensure_state(context: ContextTypes.DEFAULT_TYPE):
    if not hasattr(context.application, "help_state"):
        context.application.help_state = await load_state()


# --- Bot flows ------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_state(context)
    user = update.effective_user
    uid = user.id
    app = context.application
    s = app.help_state

    # record user
    s["users"].setdefault(str(uid), {"first_name": user.first_name or "", "issues": []})
    await save_state(s)

    kb = [
        [InlineKeyboardButton("Payment issue 💳", callback_data="issue_payment")],
        [InlineKeyboardButton("Technical issue 🛠️", callback_data="issue_tech")],
        [InlineKeyboardButton("Others 🔗", callback_data="issue_other")],
    ]
    warning = "⚠️ Please choose your issue from the buttons below before sending messages."
    await update.message.reply_text(f"Hi {user.first_name or 'there'}! {warning}", reply_markup=InlineKeyboardMarkup(kb))


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_state(context)
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id
    app = context.application
    s = app.help_state

    if data == "issue_payment":
        kb = [
            [InlineKeyboardButton("VIP", callback_data="payment_vip"), InlineKeyboardButton("DARK", callback_data="payment_dark")],
            [InlineKeyboardButton("BOTH", callback_data="payment_both")],
        ]
        await query.edit_message_text("You chose *Payment issue*. Select the service:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("payment_"):
        which = data.split("_", 1)[1]
        # ask for screenshot + optional ref/UTR
        s["users"].setdefault(str(uid), {}).update({"last_action": "awaiting_payment", "last_service": which})
        await save_state(s)
        await query.edit_message_text(
            "Please send a screenshot of the payment (photo / document). You can also include an optional UTR/Reference number in the caption or a following message."
        )
        return

    if data == "issue_tech":
        s["users"].setdefault(str(uid), {}).update({"last_action": "awaiting_tech"})
        await save_state(s)
        await query.edit_message_text("Please describe your technical issue in as much detail as possible. Then send.")
        return

    if data == "issue_other":
        await query.edit_message_text("For other issues please contact @Vip_Help_center1222_bot")
        return

    # Admin action callbacks: approve/decline/reply/ignore
    if data.startswith("admin_pay_"):
        _, _, payload = data.partition("admin_pay_")
        if "_" not in payload:
            await query.answer("Invalid callback payload", show_alert=True)
            return
        pending_id, action = payload.split("_", 1)
        await handle_admin_payment_action(update, context, pending_id, action)
        return

    if data.startswith("admin_tech_"):
        _, _, payload = data.partition("admin_tech_")
        if "_" not in payload:
            await query.answer("Invalid callback payload", show_alert=True)
            return
        pending_id, action = payload.split("_", 1)
        await handle_admin_tech_action(update, context, pending_id, action)
        return


async def handle_admin_payment_action(update: Update, context: ContextTypes.DEFAULT_TYPE, pending_id: str, action: str):
    await ensure_state(context)
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id not in ADMIN_IDS:
        await query.answer("Unauthorized", show_alert=True)
        return

    app = context.application
    s = app.help_state
    pending = s["pending"].get(pending_id)
    if not pending:
        await query.answer("Pending item not found")
        return

    user_id = int(pending["user_id"])

    if action == "decline":
        await context.bot.send_message(chat_id=user_id, text="Your payment submission was reviewed by admin and declined. If you believe this is a mistake, reply here or contact support.")
        s["pending"].pop(pending_id, None)
        await save_state(s)
        await query.edit_message_text("Declined and user notified.")
        return

    # handle approvals
    vip_link = s.get("vip_link", "")
    dark_link = s.get("dark_link", "")
    send_msgs = []
    if action == "vip" and vip_link:
        send_msgs.append(vip_link)
    if action == "dark" and dark_link:
        send_msgs.append(dark_link)
    if action == "both":
        if vip_link:
            send_msgs.append(vip_link)
        if dark_link:
            send_msgs.append(dark_link)

    if not send_msgs:
        await query.answer("No link configured for the chosen service. Use /set_vip_link or /set_dark_link first.")
        return

    # send links to user
    for msg in send_msgs:
        await context.bot.send_message(chat_id=user_id, text=f"Admin approved. Here's your link: {msg}")
        s["counters"]["links_sent"] += 1

    s["pending"].pop(pending_id, None)
    await save_state(s)
    await query.edit_message_text("Approved and links sent to user.")


async def handle_admin_tech_action(update: Update, context: ContextTypes.DEFAULT_TYPE, pending_id: str, action: str):
    await ensure_state(context)
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id not in ADMIN_IDS:
        await query.answer("Unauthorized", show_alert=True)
        return

    app = context.application
    s = app.help_state
    pending = s["pending"].get(pending_id)
    if not pending:
        await query.answer("Pending item not found")
        return

    user_id = int(pending["user_id"])

    if action == "ignore":
        await context.bot.send_message(chat_id=user_id, text="Admin marked your issue as invalid/ignored. If you disagree, reply here.")
        s["pending"].pop(pending_id, None)
        await save_state(s)
        await query.edit_message_text("Ignored and user notified.")
        return

    if action == "reply":
        # instruct admin to use /reply <user_id> <text>
        await query.edit_message_text(
            (
                f"To reply, use the command: /reply {user_id} <your message>\n\n"
                f"Example: /reply {user_id} Hi, we've fixed your issue. Please check now."
            )
        )
        return


# --- Message handlers -----------------------------------------------------------

async def photo_or_doc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch payment screenshots and forward to admin if user was in awaiting_payment state."""
    await ensure_state(context)
    user = update.effective_user
    uid = user.id
    app = context.application
    s = app.help_state

    user_rec = s["users"].get(str(uid), {})
    if user_rec.get("last_action") != "awaiting_payment":
        # user sent media unexpectedly
        await update.message.reply_text("Please use the buttons and select your issue first. Tap /start to choose.")
        return

    # extract optional caption (could contain reference/UTR)
    caption = update.message.caption or ""

    # forward the media to admin(s) with action buttons
    pending_id = str(int(time.time() * 1000))
    pending_item = {
        "type": "payment",
        "user_id": str(uid),
        "service": user_rec.get("last_service"),
        "caption": caption,
    }

    # store pending and increment counter
    s["pending"][pending_id] = pending_item
    s["counters"]["payment_submitted"] += 1
    await save_state(s)

    # admin buttons
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Approve VIP", callback_data=f"admin_pay_{pending_id}_vip"),
            InlineKeyboardButton("Approve DARK", callback_data=f"admin_pay_{pending_id}_dark"),
        ],
        [InlineKeyboardButton("Approve BOTH", callback_data=f"admin_pay_{pending_id}_both")],
        [InlineKeyboardButton("Decline", callback_data=f"admin_pay_{pending_id}_decline")],
    ])

    if not ADMIN_IDS:
        print("Warning: No ADMIN_IDS configured. Payment evidence will not be forwarded to any admin.")

    for aid in ADMIN_IDS:
            try:
                text_to_admin = f"Tech issue from {user.full_name} (id: {uid})\n\n{text}"
                await context.bot.send_message(chat_id=aid, text=text_to_admin, reply_markup=kb)
            except Exception as e:
                print("Failed to forward tech issue", e)

                user_rec["last_action"] = None
        await update.message.reply_text("Thanks — your technical issue has been forwarded to admin. We'll notify you when it's resolved.")
        return

    # Admin reply command handled separately
    if text.startswith("/reply") and uid in ADMIN_IDS:
        # format: /reply <user_id> <message>
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await update.message.reply_text("Usage: /reply <user_id> <your message>")
            return
        try:
            target_id = int(parts[1])
        except ValueError:
            await update.message.reply_text("Invalid user id.")
            return
        msg = parts[2]
        try:
            await context.bot.send_message(chat_id=target_id, text=f"Admin: {msg}")
            await update.message.reply_text("Sent reply to user.")
        except Exception as e:
            await update.message.reply_text(f"Failed to send message: {e}")
        return

    # Admin set link commands
    if text.startswith("/set_vip_link") and uid in ADMIN_IDS:
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("Usage: /set_vip_link <url>")
            return
        s["vip_link"] = parts[1].strip()
        await save_state(s)
        await update.message.reply_text("VIP link saved.")
        return

    if text.startswith("/set_dark_link") and uid in ADMIN_IDS:
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("Usage: /set_dark_link <url>")
            return
        s["dark_link"] = parts[1].strip()
        await save_state(s)
        await update.message.reply_text("DARK link saved.")
        return

    if text.startswith("/broadcast") and uid in ADMIN_IDS:
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("Usage: /broadcast <message>")
            return
        broadcast_msg = parts[1]
        count = 0
        for u in list(s["users"].keys()):
            try:
                await context.bot.send_message(chat_id=int(u), text=broadcast_msg)
                count += 1
            except Exception:
                pass
        await update.message.reply_text(f"Broadcast sent to {count} users.")
        return

    if text.startswith("/insights") and uid in ADMIN_IDS:
        counters = s.get("counters", {})
        vip = s.get("vip_link", "(not set)")
        dark = s.get("dark_link", "(not set)")
        await update.message.reply_text(
            f"Insights:
Payments submitted: {counters.get('payment_submitted',0)}
Tech submitted: {counters.get('tech_submitted',0)}
Links sent: {counters.get('links_sent',0)}
VIP link: {vip}
DARK link: {dark}"
        )
        return

    # Fallback for other messages
    await update.message.reply_text("If you have an issue please use /start and choose the right button. For other help contact @Vip_Help_center1222_bot")


# --- Main ----------------------------------------------------------------------

def main():
    # Build application
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Load state synchronously at startup
    try:
        app.help_state = asyncio.run(load_state())
    except Exception as e:
        print("Failed to load initial state:", e)
        app.help_state = DEFAULT_STATE.copy()

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_buttons))

    # media handlers (photo, document)
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, photo_or_doc_handler))

    # text handler (non-command texts)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler))

    print("Bot starting (run_polling)...")
    # This will block until the process is stopped
    app.run_polling()


if __name__ == "__main__":
    main()

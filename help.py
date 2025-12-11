#!/usr/bin/env python3
"""
Telegram Help Center Bot
- Python 3.11.11
- Requires: python-telegram-bot==20.7

Features:
- Payment and Technical issue flows (text + media)
- Admin panel with Set VIP / Set DARK / Set BOTH / Broadcast / Insights / Get Links
- Admin quick-reply flow for tech issues (Reply -> next admin message forwarded)
- Admin commands: /set_vip_link, /set_dark_link, /set_both_link, /get_links, /admin, /broadcast, /insights, /reply
- /cancel (admin) cancels quick-reply or any admin session
- Persistence in DATA_DIR/helpcenter_state.json
"""

import os
import json
import time
from functools import wraps
from typing import Dict, Any
from pathlib import Path

from telegram.error import BadRequest


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
                print("Warning: invalid ADMIN_CHAT_ID part:", part)

# Create data dir
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

DEFAULT_STATE = {
    "users": {},
    "pending": {},
    "vip_link": "",
    "dark_link": "",
    "counters": {"payment_submitted": 0, "tech_submitted": 0, "links_sent": 0},
}


def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print("Failed to load state, using defaults:", e)
            return DEFAULT_STATE.copy()
    return DEFAULT_STATE.copy()


def save_state(s: Dict[str, Any]):
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
            try:
                if getattr(update, "message", None):
                    await update.message.reply_text("Unauthorized: admin only.")
                elif getattr(update, "callback_query", None):
                    await update.callback_query.answer("Unauthorized", show_alert=True)
            except Exception:
                pass
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


async def ensure_state(context: ContextTypes.DEFAULT_TYPE):
    # populate application-level transient attributes
    if not hasattr(context.application, "help_state"):
        context.application.help_state = load_state()
    if not hasattr(context.application, "admin_sessions"):
        context.application.admin_sessions = {}


# --- Admin helper functions ----------------------------------------------------

async def send_admin_panel(admin_id: int, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Set VIP link", callback_data="adminpanel_set_vip"),
             InlineKeyboardButton("Set DARK link", callback_data="adminpanel_set_dark")],
            [InlineKeyboardButton("Set BOTH links", callback_data="adminpanel_set_both")],
            [InlineKeyboardButton("Broadcast", callback_data="adminpanel_broadcast"),
             InlineKeyboardButton("Insights", callback_data="adminpanel_insights")],
            [InlineKeyboardButton("Get Links", callback_data="adminpanel_get_links")],
        ]
    )
    try:
        await context.bot.send_message(chat_id=admin_id, text="Admin panel — choose an action:", reply_markup=kb)
    except Exception as e:
        print("Failed to send admin panel to", admin_id, e)


# --- Bot flows ------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_state(context)
    user = update.effective_user
    uid = user.id
    app = context.application
    s = app.help_state

    s["users"].setdefault(str(uid), {"first_name": user.first_name or "", "issues": []})
    save_state(s)

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

    # user flows
    if data == "issue_payment":
        kb = [
            [InlineKeyboardButton("VIP", callback_data="payment_vip"),
             InlineKeyboardButton("DARK", callback_data="payment_dark")],
            [InlineKeyboardButton("BOTH", callback_data="payment_both")],
        ]
        await query.edit_message_text("You chose *Payment issue*. Select the service:", parse_mode="Markdown",
                                      reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("payment_"):
        which = data.split("_", 1)[1]
        s["users"].setdefault(str(uid), {}).update({"last_action": "awaiting_payment", "last_service": which})
        save_state(s)
        await query.edit_message_text(
            "Please send a screenshot of the payment (photo / document). You can also include an optional UTR/Reference number in the caption or a following message."
        )
        return

    if data == "issue_tech":
        s["users"].setdefault(str(uid), {}).update({"last_action": "awaiting_tech"})
        save_state(s)
        await query.edit_message_text("Please describe your technical issue in as much detail as possible. Then send.")
        return

    if data == "issue_other":
        await query.edit_message_text("For other issues please contact @Vip_Help_center1222_bot")
        return

    # admin panel callbacks
    if data.startswith("adminpanel_"):
        if uid not in ADMIN_IDS:
            await query.answer("Unauthorized", show_alert=True)
            return
        action = data.split("adminpanel_", 1)[1]
        if action == "set_vip":
            context.application.admin_sessions[uid] = {"action": "set_vip"}
            await query.edit_message_text("Send the VIP link now (just paste the URL as a message).")
            return
        if action == "set_dark":
            context.application.admin_sessions[uid] = {"action": "set_dark"}
            await query.edit_message_text("Send the DARK link now (just paste the URL as a message).")
            return
        if action == "set_both":
            context.application.admin_sessions[uid] = {"action": "set_both"}
            await query.edit_message_text("Send VIP and DARK links separated by a space (VIP_URL DARK_URL) or newline.")
            return
        if action == "broadcast":
            context.application.admin_sessions[uid] = {"action": "broadcast"}
            await query.edit_message_text("Send the broadcast message text now. It will be sent to all recorded users.")
            return
        if action == "insights":
            counters = s.get("counters", {})
            insights_msg = (
                "Insights:\n"
                f"Payments submitted: {counters.get('payment_submitted', 0)}\n"
                f"Tech submitted: {counters.get('tech_submitted', 0)}\n"
                f"Links sent: {counters.get('links_sent', 0)}\n"
                f"VIP link: {s.get('vip_link', '(not set)')}\n"
                f"DARK link: {s.get('dark_link', '(not set)')}"
            )
            await query.edit_message_text(insights_msg)
            return
        if action == "get_links":
            links_msg = (
                f"VIP link: {s.get('vip_link', '(not set)')}\n"
                f"DARK link: {s.get('dark_link', '(not set)')}"
            )
            await query.edit_message_text(links_msg)
            return

    # admin approve/decline payment
    if data.startswith("admin_pay_"):
        _, _, payload = data.partition("admin_pay_")
        if "_" not in payload:
            await query.answer("Invalid callback payload", show_alert=True)
            return
        pending_id, action = payload.split("_", 1)
        await handle_admin_payment_action(update, context, pending_id, action)
        return

    # admin tech callbacks (reply/ignore)
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
        await context.bot.send_message(chat_id=user_id,
                                       text="Your payment submission was reviewed by admin and declined. If you believe this is a mistake, reply here or contact support.")
        s["pending"].pop(pending_id, None)
        save_state(s)
        await query.edit_message_text("Declined and user notified.")
        return

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

    for msg in send_msgs:
        await context.bot.send_message(chat_id=user_id, text=f"Admin approved. Here's your link: {msg}")
        s["counters"]["links_sent"] += 1

    s["pending"].pop(pending_id, None)
    save_state(s)
    await query.edit_message_text("Approved and links sent to user.")


async def handle_admin_tech_action(update: Update, context: ContextTypes.DEFAULT_TYPE, pending_id: str, action: str):
    """
    Handles admin actions on a tech pending item.
    - 'ignore' -> notify user, remove pending
    - 'reply'  -> switch admin into quick-reply mode; next admin message will be forwarded to target user
    """
    await ensure_state(context)
    query = update.callback_query

    # Always acknowledge the callback (important for client UX)
    try:
        await query.answer()
    except Exception:
        # ignore answer failure but continue
        pass

    admin_id = query.from_user.id
    if admin_id not in ADMIN_IDS:
        try:
            await query.answer("Unauthorized", show_alert=True)
        except Exception:
            pass
        return

    app = context.application
    s = app.help_state

    pending = s.get("pending", {}).get(pending_id)
    if not pending:
        try:
            await query.answer("Pending item not found", show_alert=True)
        except Exception:
            pass
        # also try to edit the admin message so it's not left with stale buttons
        try:
            await query.edit_message_text("This pending item was already handled or expired.")
        except Exception:
            pass
        return

    # parse target user id
    try:
        user_id = int(pending["user_id"])
    except Exception:
        user_id = None

    if action == "ignore":
        # Notify user and cleanup
        try:
            if user_id:
                await context.bot.send_message(chat_id=user_id, text="Admin marked your issue as invalid/ignored. If you disagree, reply here.")
        except Exception as e:
            # tell admin about failure to notify the user
            try:
                await query.edit_message_text(f"Ignored, but failed to notify user: {e}")
            except Exception:
                pass
            # still remove pending if present
            s.get("pending", {}).pop(pending_id, None)
            save_state(s)
            return

        # remove pending and persist
        s.get("pending", {}).pop(pending_id, None)
        save_state(s)

        # update admin message
        try:
            await query.edit_message_text("Ignored and user notified.")
        except Exception:
            # if editing fails, at least send a short callback answer
            try:
                await query.answer("Ignored and user notified.", show_alert=False)
            except Exception:
                pass
        return

    if action == "reply":
        # put admin into quick-reply mode (next message will be forwarded)
        context.application.admin_sessions[admin_id] = {
            "action": "quick_reply",
            "target_user": user_id,
            "pending_id": pending_id,
            "started_at": int(time.time())
        }

        # show admin that quick-reply is active and provide a Cancel button
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("Cancel quick-reply", callback_data=f"admin_quick_cancel_{admin_id}")]])
        try:
            await query.edit_message_text(
                "Quick-reply mode: send the reply message here and it will be forwarded to the user.\nTo cancel, press the button below or send /cancel.",
                reply_markup=cancel_kb
            )
        except Exception:
            # fallback: at least answer the callback
            try:
                await query.answer("Quick-reply mode enabled. Send your reply or /cancel.")
            except Exception:
                pass
        return

    # unknown action fallback
    try:
        await query.answer("Unknown action", show_alert=True)
    except Exception:
        pass

async def safe_edit_admin_message(query, text, reply_markup=None):
    """
    Try to update the admin message in a safe order:
      1) edit_message_text (if message had text)
      2) edit_message_caption (if message was media with caption)
      3) edit_message_reply_markup (remove buttons)
      4) fallback to answering the callback
    """
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
        return
    except BadRequest as e:
        # common case: "There is no text in the message to edit"
        msg = str(e)
        # try edit caption if it's a media message
        try:
            await query.edit_message_caption(caption=text, reply_markup=reply_markup)
            return
        except BadRequest:
            # maybe there is no caption or caption can't be edited; try removing markup
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                # last fallback: answer callback so admin sees confirmation
                try:
                    await query.answer(text)
                except Exception:
                    pass
            return
    except Exception:
        # last fallback: answer callback
        try:
            await query.answer(text)
        except Exception:
            pass

# --- New: quick-cancel callback handler ----------------------------------------

async def handle_quick_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles the Cancel quick-reply inline button.
    callback_data expected: admin_quick_cancel_<admin_id>
    """
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    data = query.data or ""
    parts = data.split("_")
    admin_id = None
    if len(parts) >= 4:
        try:
            admin_id = int(parts[-1])
        except Exception:
            admin_id = None

    if admin_id is None or admin_id not in ADMIN_IDS:
        try:
            await query.edit_message_text("Unauthorized to cancel.")
        except Exception:
            try:
                await query.answer("Unauthorized", show_alert=True)
            except Exception:
                pass
        return

    # remove session if present
    session = context.application.admin_sessions.pop(admin_id, None)

    try:
        await query.edit_message_text("Quick-reply cancelled.")
    except Exception:
        try:
            await query.answer("Quick-reply cancelled.")
        except Exception:
            pass


# --- Message / media handlers ---------------------------------------------------

async def photo_or_doc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle incoming photos/documents for both payment and technical issue flows.
    """
    await ensure_state(context)
    user = update.effective_user
    uid = user.id
    app = context.application
    s = app.help_state

    user_rec = s["users"].get(str(uid), {})
    last_action = user_rec.get("last_action")

    if last_action not in ("awaiting_payment", "awaiting_tech"):
        await update.message.reply_text("Please choose your issue from /start and tap the buttons before sending media.")
        return

    caption = update.message.caption or ""
    pending_id = str(int(time.time() * 1000))

    if last_action == "awaiting_payment":
        pending_item = {
            "type": "payment",
            "user_id": str(uid),
            "service": user_rec.get("last_service"),
            "caption": caption,
        }
        s["pending"][pending_id] = pending_item
        s["counters"]["payment_submitted"] = s.get("counters", {}).get("payment_submitted", 0) + 1
        save_state(s)

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Approve VIP", callback_data=f"admin_pay_{pending_id}_vip"),
             InlineKeyboardButton("Approve DARK", callback_data=f"admin_pay_{pending_id}_dark")],
            [InlineKeyboardButton("Approve BOTH", callback_data=f"admin_pay_{pending_id}_both")],
            [InlineKeyboardButton("Decline", callback_data=f"admin_pay_{pending_id}_decline")],
        ])

        if not ADMIN_IDS:
            print("Warning: No ADMIN_IDS configured. Payment evidence will not be forwarded to any admin.")

        for aid in ADMIN_IDS:
            try:
                caption_text = (
                    f"Payment from {user.full_name} (id: {uid})\n"
                    f"Service: {pending_item['service']}\n"
                    f"Caption: {caption}"
                )
                if update.message.photo:
                    await context.bot.send_photo(chat_id=aid, photo=update.message.photo[-1].file_id, caption=caption_text, reply_markup=kb)
                elif update.message.document:
                    await context.bot.send_document(chat_id=aid, document=update.message.document.file_id, caption=caption_text, reply_markup=kb)
                else:
                    await context.bot.send_message(chat_id=aid, text=caption_text, reply_markup=kb)
            except Exception as e:
                print("Failed to forward to admin", aid, e)

        user_rec["last_action"] = None
        save_state(s)
        await update.message.reply_text("Thanks — your payment evidence has been sent to admin for review. We'll notify you when it's processed.")
        return

    # awaiting_tech
    pending_item = {
        "type": "tech",
        "user_id": str(uid),
        "caption": caption,
        "has_media": True,
    }
    s["pending"][pending_id] = pending_item
    s["counters"]["tech_submitted"] = s.get("counters", {}).get("tech_submitted", 0) + 1
    save_state(s)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Reply to user", callback_data=f"admin_tech_{pending_id}_reply"),
         InlineKeyboardButton("Ignore", callback_data=f"admin_tech_{pending_id}_ignore")]
    ])

    if not ADMIN_IDS:
        print("Warning: No ADMIN_IDS configured. Tech media will not be forwarded to any admin.")

    for aid in ADMIN_IDS:
        try:
            caption_text = (
                f"Tech issue (media) from {user.full_name} (id: {uid})\n"
                f"Caption: {caption}"
            )
            if update.message.photo:
                await context.bot.send_photo(chat_id=aid, photo=update.message.photo[-1].file_id, caption=caption_text, reply_markup=kb)
            elif update.message.document:
                await context.bot.send_document(chat_id=aid, document=update.message.document.file_id, caption=caption_text, reply_markup=kb)
            else:
                await context.bot.send_message(chat_id=aid, text=caption_text, reply_markup=kb)
        except Exception as e:
            print("Failed to forward tech media to admin", aid, e)

    user_rec["last_action"] = None
    save_state(s)
    await update.message.reply_text("Thanks — your technical issue (media) has been forwarded to admin. We'll notify you when it's resolved.")
    return


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_state(context)
    user = update.effective_user
    uid = user.id
    app = context.application
    s = app.help_state
    text = update.message.text or ""

    # allow admins to cancel any pending admin session (/cancel)
    if text.strip().lower() == "/cancel" and uid in ADMIN_IDS:
        session = context.application.admin_sessions.pop(uid, None)
        if session:
            await update.message.reply_text("Admin session cancelled.")
        else:
            await update.message.reply_text("No active admin session.")
        return

    # check admin session first (quick-reply, set_vip, etc.)
    admin_session = getattr(context.application, "admin_sessions", {}).get(uid)
    if admin_session and uid in ADMIN_IDS:
        action = admin_session.get("action")
        # quick-reply: forward message to the target user
        if action == "quick_reply":
            target_user = admin_session.get("target_user")
            pending_id = admin_session.get("pending_id")
            try:
                # forward the admin's text as reply
                if not target_user:
                    await update.message.reply_text("No target user found; cancelling session.")
                    context.application.admin_sessions.pop(uid, None)
                    return
                await context.bot.send_message(chat_id=target_user, text=f"Admin: {text}")
                await update.message.reply_text("Reply sent to user.")
            except Exception as e:
                await update.message.reply_text(f"Failed to send reply: {e}")
            # remove pending item if exists
            try:
                if pending_id and pending_id in s.get("pending", {}):
                    s["pending"].pop(pending_id, None)
                    save_state(s)
            except Exception:
                pass
            context.application.admin_sessions.pop(uid, None)
            return

        # handle set_vip / set_dark / set_both / broadcast flows
        if action == "set_vip":
            s["vip_link"] = text.strip()
            save_state(s)
            await update.message.reply_text("VIP link saved.")
            context.application.admin_sessions.pop(uid, None)
            return
        if action == "set_dark":
            s["dark_link"] = text.strip()
            save_state(s)
            await update.message.reply_text("DARK link saved.")
            context.application.admin_sessions.pop(uid, None)
            return
        if action == "set_both":
            parts = text.strip().split()
            if len(parts) >= 2:
                s["vip_link"] = parts[0].strip()
                s["dark_link"] = parts[1].strip()
                save_state(s)
                await update.message.reply_text("VIP and DARK links saved.")
            else:
                await update.message.reply_text("Please send two URLs separated by a space or newline: VIP_URL DARK_URL")
                return
            context.application.admin_sessions.pop(uid, None)
            return
        if action == "broadcast":
            broadcast_msg = text
            count = 0
            for u in list(s["users"].keys()):
                try:
                    await context.bot.send_message(chat_id=int(u), text=broadcast_msg)
                    count += 1
                except Exception:
                    pass
            await update.message.reply_text(f"Broadcast sent to {count} users.")
            context.application.admin_sessions.pop(uid, None)
            return

    # now normal user flows and admin commands routed through same handler
    user_rec = s["users"].setdefault(str(uid), {})
    if user_rec.get("last_action") is None:
        # allow admin commands even if user hasn't clicked buttons
        if text.startswith("/"):
            # continue to admin command handling below
            pass
        else:
            await update.message.reply_text("⚠️ Please choose your issue using /start and tap the buttons before messaging. This helps us fast-track your request.")
            return

    # If user was awaiting tech (text) -> forward to admin
    if user_rec.get("last_action") == "awaiting_tech":
        pending_id = str(int(time.time() * 1000))
        pending_item = {"type": "tech", "user_id": str(uid), "text": text, "has_media": False}
        s["pending"][pending_id] = pending_item
        s["counters"]["tech_submitted"] = s.get("counters", {}).get("tech_submitted", 0) + 1
        save_state(s)

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Reply to user", callback_data=f"admin_tech_{pending_id}_reply"),
             InlineKeyboardButton("Ignore", callback_data=f"admin_tech_{pending_id}_ignore")]
        ])

        if not ADMIN_IDS:
            print("Warning: No ADMIN_IDS configured. Tech issues will not be forwarded to any admin.")

        for aid in ADMIN_IDS:
            try:
                text_to_admin = (
                    f"Tech issue from {user.full_name} (id: {uid})\n\n"
                    f"{text}"
                )
                await context.bot.send_message(chat_id=aid, text=text_to_admin, reply_markup=kb)
            except Exception as e:
                print("Failed to forward tech issue", e)

        user_rec["last_action"] = None
        save_state(s)
        await update.message.reply_text("Thanks — your technical issue has been forwarded to admin. We'll notify you when it's resolved.")
        return

    # Admin typed a /reply (legacy direct command) OR other admin commands
    # Process admin-only commands below
    if text.startswith("/reply") and uid in ADMIN_IDS:
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

    if text.startswith("/set_vip_link") and uid in ADMIN_IDS:
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("Usage: /set_vip_link <url>")
            return
        s["vip_link"] = parts[1].strip()
        save_state(s)
        await update.message.reply_text("VIP link saved.")
        return

    if text.startswith("/set_dark_link") and uid in ADMIN_IDS:
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("Usage: /set_dark_link <url>")
            return
        s["dark_link"] = parts[1].strip()
        save_state(s)
        await update.message.reply_text("DARK link saved.")
        return

    if text.startswith("/set_both_link") and uid in ADMIN_IDS:
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await update.message.reply_text("Usage: /set_both_link <vip_url> <dark_url>")
            return
        s["vip_link"] = parts[1].strip()
        s["dark_link"] = parts[2].strip()
        save_state(s)
        await update.message.reply_text("VIP and DARK links saved.")
        return

    if text.startswith("/get_links") and uid in ADMIN_IDS:
        get_links_msg = (
            f"VIP link: {s.get('vip_link','(not set)')}\n"
            f"DARK link: {s.get('dark_link','(not set)')}"
        )
        await update.message.reply_text(get_links_msg)
        return

    if text.startswith("/admin") and uid in ADMIN_IDS:
        await send_admin_panel(uid, context)
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
        insights_msg = (
            "Insights:\n"
            f"Payments submitted: {counters.get('payment_submitted', 0)}\n"
            f"Tech submitted: {counters.get('tech_submitted', 0)}\n"
            f"Links sent: {counters.get('links_sent', 0)}\n"
            f"VIP link: {vip}\n"
            f"DARK link: {dark}"
        )
        await update.message.reply_text(insights_msg)
        return

    # fallback
    await update.message.reply_text("If you have an issue please use /start and choose the right button. For other help contact @Vip_Help_center1222_bot")


# --- Main ----------------------------------------------------------------------

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Load state synchronously at startup
    try:
        app.help_state = load_state()
    except Exception as e:
        print("Failed to load initial state:", e)
        app.help_state = DEFAULT_STATE.copy()

    # ensure admin_sessions exists
    if not hasattr(app, "admin_sessions"):
        app.admin_sessions = {}

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    # route commands to the same text handler (your text_handler checks permissions)
    app.add_handler(MessageHandler(filters.COMMAND, text_handler))

    # Register quick-cancel callback BEFORE generic button handler so pattern matches first
    app.add_handler(CallbackQueryHandler(handle_quick_cancel, pattern=r"^admin_quick_cancel_"))

    app.add_handler(CallbackQueryHandler(handle_buttons))

    # media handlers (photo, document) - handles both payment & tech media
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, photo_or_doc_handler))

    # text handler (non-command texts)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler))

    print("Bot starting (run_polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()

from __future__ import annotations
"""
bot.py — smuHBLogs Telegram Bot
Handball team logistics tracker for SMU.

Commands
────────
Public (anyone can DM the bot):
  /start               — welcome + command list
  /inventory           — see all holdings
  /inventory [item]    — who has a specific item
  /whohas [name]       — what someone is holding
  /acceptic            — accept a pending IC handover

IC-only:
  /setholding [name] [qty?] [item]
  /removeitem [name] [item]
  /transfer [item] from [name] to [name]
  /update [name] [qty?] [item], ...     ← bulk post-training update
  /training [DD/MM/YYYY] [venue] [time]
  /attendance                           ← reply to the attendance msg
  /required [items, ...]
  /delegate                             ← generate delegation plan + copy-paste message
  /clear training|inventory|all
  /handover @username
  /listic

Master-only:
  /removeic @username
"""

import os
import re
import json
import time
import logging
from datetime import datetime, date
from collections import defaultdict, deque
from dotenv import load_dotenv
load_dotenv()
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

import database as db

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MASTER_ID    = int(os.getenv("MASTER_ID", "605114234"))
BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set.")

from groq import Groq
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# Rate limit: max 5 Groq calls per user per 60 seconds
_GROQ_RATE_LIMIT = 5
_GROQ_RATE_WINDOW = 60
_groq_calls: dict[int, deque] = defaultdict(deque)

def _check_groq_rate_limit(user_id: int) -> bool:
    """Returns True if the user is allowed to make a Groq call, False if rate-limited."""
    now = time.monotonic()
    q = _groq_calls[user_id]
    while q and now - q[0] > _GROQ_RATE_WINDOW:
        q.popleft()
    if len(q) >= _GROQ_RATE_LIMIT:
        return False
    q.append(now)
    return True


# ──────────────────────────────────────────────────────────────
# PARSE HELPERS
# ──────────────────────────────────────────────────────────────

def fmt(item: str, qty: int) -> str:
    """Format item with quantity. '4x balls' or just 'bibs'."""
    return f"{qty}x {item}" if qty > 1 else item


def parse_name_qty_item(tokens: list[str]) -> tuple[str, int, str]:
    """
    First token = name. Rest = optional qty + item name.
    ['ella', '4', 'balls']    → ('ella', 4, 'balls')
    ['rena', 'bibs']          → ('rena', 1, 'bibs')
    ['eunice', 'tape', 'bag'] → ('eunice', 1, 'tape bag')
    """
    if not tokens:
        return "", 1, ""
    name = tokens[0]
    rest = tokens[1:]
    if rest and rest[0].isdigit():
        return name, int(rest[0]), " ".join(rest[1:])
    return name, 1, " ".join(rest)


def parse_items_list(raw: str) -> list[tuple[str, int]]:
    """
    Comma-separated items with optional leading quantity.
    '10 balls, bibs, tape bag, 2 cones'
    → [('balls', 10), ('bibs', 1), ('tape bag', 1), ('cones', 2)]
    """
    result = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        tokens = part.split()
        if tokens and tokens[0].isdigit():
            qty, item = int(tokens[0]), " ".join(tokens[1:])
        else:
            qty, item = 1, " ".join(tokens)
        if item:
            result.append((item, qty))
    return result


def parse_attendance_text(text: str) -> list[tuple[str, str, str | None]]:
    """
    Parse an attendance message into [(name, status, late_time), ...].

    Handles:
      Ruhan (late, 9)   → ('ruhan', 'late', '9')
      Ally              → ('ally', 'present', None)

    Skips metadata lines (Attendance for..., Venue:, Reporting time:, dashes).
    """
    attendees = []
    skip_prefixes = ("attendance", "venue:", "reporting time:", "time:", "location:", "-", "/")
    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(line.lower().startswith(kw) for kw in skip_prefixes):
            continue
        # Late pattern: "Ruhan (late, 9)" or "Ruhan (late 9pm)"
        late_m = re.match(r"^([A-Za-z]+)\s*\(late[,\s]+([^\)]+)\)", line, re.IGNORECASE)
        if late_m:
            attendees.append((late_m.group(1), "late", late_m.group(2).strip()))
            continue
        # Regular name — grab the first word (handles "Ally 🏐" etc.)
        name_m = re.match(r"^([A-Za-z]+)", line)
        if name_m:
            attendees.append((name_m.group(1), "present", None))
    return attendees


def parse_attendance_forward(text: str):
    """
    Detect and parse a forwarded attendance message like:
        Attendance 18/03/26
        name1
        name2
        Location: MPSH
        Time: 745PM

    Returns (date_str, venue, time_str, attendees) or None if not recognised.
    date_str is in DD/MM/YYYY format.
    """
    lines = text.strip().splitlines()
    if not lines:
        return None

    # First non-empty line must be "Attendance DD/MM/YY[YY]"
    first = lines[0].strip()
    date_m = re.match(
        r"^Attendance\s+(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})",
        first, re.IGNORECASE,
    )
    if not date_m:
        return None

    day, month, year = date_m.group(1), date_m.group(2), date_m.group(3)
    if len(year) == 2:
        year = "20" + year
    date_str = f"{day.zfill(2)}/{month.zfill(2)}/{year}"

    venue, time_str = None, None
    for line in lines[1:]:
        line = line.strip()
        loc_m = re.match(r"^Location:\s*(.+)", line, re.IGNORECASE)
        if loc_m:
            venue = loc_m.group(1).strip()
            continue
        time_m = re.match(r"^Time:\s*(.+)", line, re.IGNORECASE)
        if time_m:
            time_str = time_m.group(1).strip()

    if not venue or not time_str:
        return None

    attendees = parse_attendance_text(text)
    return date_str, venue, time_str, attendees


# ──────────────────────────────────────────────────────────────
# ACCESS DECORATORS
# ──────────────────────────────────────────────────────────────

def ic_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not db.is_ic_or_master(update.effective_user.id):
            await update.message.reply_text("🔒 IC or master access required.")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


def master_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if db.get_role(update.effective_user.id) != "master":
            await update.message.reply_text("🔒 Master access required.")
            return
        return await func(update, context)
    wrapper.__name__ = func.__name__
    return wrapper


# ──────────────────────────────────────────────────────────────
# PUBLIC COMMANDS
# ──────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    role  = db.get_role(update.effective_user.id) or "viewer"
    badge = {"master": "👑 Master", "ic": "🔑 IC", "viewer": "👁 Viewer"}.get(role, role)
    is_ic = role in ("ic", "master")

    lines = [f"*smuHBLogs* — {badge}\n"]

    if is_ic:
        training = db.get_active_training()

        if not training:
            lines += [
                "No upcoming training set.\n",
                "To get started:",
                "• Forward an attendance message here — the bot will set it up automatically",
                "• Or manually: `/training DD/MM/YYYY venue time`\n",
                "📦 `/inventory` — check current equipment",
            ]
            keyboard = [["/inventory", "/whohas"], ["/training", "/help"]]
        else:
            attendance = db.get_attendance_rows(training["id"])
            required   = db.get_required_items(training["id"])

            lines += [
                f"📅 *{training['date']}* · {training['venue']} · {training['report_time']}\n",
            ]

            # Attendance status
            if attendance:
                present_count = sum(1 for r in attendance if r["status"] != "absent")
                lines.append(f"✅ Attendance: {present_count} people")
            else:
                lines.append("❌ Attendance not set")

            # Required items status
            if required:
                lines.append(f"✅ Required items: {len(required)} item(s)")
            else:
                lines.append("❌ Required items not set")

            lines.append("")

            # Single clear "what to do next"
            if not attendance:
                lines += [
                    "*Next step:* Set attendance",
                    "Forward the attendance message here, or reply to it with `/attendance`",
                ]
                keyboard = [["/attendance", "/inventory"], ["/required", "/help"]]
            elif not required:
                lines += [
                    "*Next step:* Set required items",
                    "`/required 10 balls, bibs, tape bag, ...`",
                ]
                keyboard = [["/required", "/delegate"], ["/inventory", "/help"]]
            else:
                lines += [
                    "*Ready to go!* Run `/delegate` to generate the equipment plan.",
                ]
                keyboard = [["/delegate", "/inventory"], ["/required", "/clear"], ["/help"]]

        lines.append("\n`/help` — all commands")
    else:
        lines += [
            "📦 `/inventory` — see all equipment holdings",
            "📦 `/inventory [item]` — who has something specific",
            "👤 `/whohas [name]` — what someone is holding",
        ]
        keyboard = [["/inventory", "/whohas"]]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=reply_markup)


async def cmd_help(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    role  = db.get_role(update.effective_user.id) or "viewer"
    is_ic = role in ("ic", "master")

    lines = ["📖 *All commands*\n"]
    lines += [
        "*Anyone:*",
        "`/inventory` — all holdings",
        "`/inventory [item]` — who has something",
        "`/whohas [name]` — what someone holds",
    ]

    if is_ic:
        lines += [
            "",
            "*Training:*",
            "`/training [DD/MM/YYYY] [venue] [time]` — create training",
            "`/attendance` — reply to attendance msg, or forward message directly",
            "`/required [items, ...]` — set what's needed",
            "`/delegate` — generate equipment plan",
            "",
            "*Inventory:*",
            "`/setholding [name] [qty?] [item]`",
            "`/removeitem [name] [item]`",
            "`/rename [old name] to [new name]`",
            "`/transfer [item] from [name] to [name]`",
            "`/update [name] [qty?] [item], ...` — bulk update",
            "",
            "*Admin:*",
            "`/clear training|inventory|all`",
            "`/handover @username`",
            "`/listic` — who has access",
        ]
        if role == "master":
            lines.append("`/removeic @username`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /inventory [optional item query]
    if context.args:
        query = " ".join(context.args)
        rows  = db.search_inventory_by_item(query)
        if not rows:
            await update.message.reply_text(
                f'❌ Nobody is currently holding *"{query}"*.',
                parse_mode="Markdown",
            )
            return
        lines = [f'📦 *Who has "{query}":*\n']
        for r in rows:
            lines.append(f"• {r['holder'].title()} — {fmt(r['item'], r['quantity'])}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    rows = db.get_full_inventory()
    if not rows:
        await update.message.reply_text(
            "📭 Inventory is empty.\n"
            "Use `/setholding` or `/update` to log who has what.",
            parse_mode="Markdown",
        )
        return

    # Group items by holder for a cleaner display
    holders: dict[str, list[str]] = {}
    for r in rows:
        holders.setdefault(r["holder"].title(), []).append(fmt(r["item"], r["quantity"]))

    lines = ["📦 *Current Inventory*\n"]
    for name, items in sorted(holders.items()):
        lines.append(f"*{name}*")
        for item in items:
            lines.append(f"  • {item}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_whohas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/whohas [name]`", parse_mode="Markdown")
        return
    name = " ".join(context.args)
    rows = db.search_inventory_by_holder(name)
    if not rows:
        await update.message.reply_text(
            f"❌ *{name.title()}* isn't holding anything right now.",
            parse_mode="Markdown",
        )
        return
    lines = [f"🎒 *{name.title()} is holding:*\n"]
    for r in rows:
        lines.append(f"• {fmt(r['item'], r['quantity'])}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ──────────────────────────────────────────────────────────────
# IC — INVENTORY MANAGEMENT
# ──────────────────────────────────────────────────────────────

@ic_only
async def cmd_setholding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/setholding [name] [qty?] [item]`\n\n"
            "Examples:\n"
            "• `/setholding ella 4 balls`\n"
            "• `/setholding rena bibs`\n"
            "• `/setholding eunice tape bag`",
            parse_mode="Markdown",
        )
        return
    name, qty, item = parse_name_qty_item(context.args)
    if not item:
        await update.message.reply_text("❌ Missing item name.")
        return
    db.set_holding(name, item, qty)
    await update.message.reply_text(
        f"✅ *{name.title()}* now holds {fmt(item, qty)}.",
        parse_mode="Markdown",
    )


@ic_only
async def cmd_removeitem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/removeitem [name] [item]`\n"
            "Example: `/removeitem nicole marker discs`",
            parse_mode="Markdown",
        )
        return
    holder = context.args[0]
    item   = " ".join(context.args[1:])
    if db.remove_holding(holder, item):
        await update.message.reply_text(
            f"🗑️ Removed *{item}* from *{holder.title()}*.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"❌ *{holder.title()}* doesn't have *{item}* in inventory.",
            parse_mode="Markdown",
        )


@ic_only
async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /rename [old name] to [new name]  OR  /rename [old] [new]
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/rename [old name] to [new name]`\n"
            "Example: `/rename sera to seraphina`",
            parse_mode="Markdown",
        )
        return

    text = " ".join(context.args)
    m = re.match(r"^(.+?)\s+to\s+(.+)$", text, re.IGNORECASE)
    if m:
        old_name, new_name = m.group(1).strip(), m.group(2).strip()
    elif len(context.args) == 2:
        old_name, new_name = context.args[0], context.args[1]
    else:
        await update.message.reply_text(
            "Usage: `/rename [old name] to [new name]`",
            parse_mode="Markdown",
        )
        return

    affected = db.rename_holder(old_name, new_name)
    if affected == 0:
        await update.message.reply_text(
            f"❌ *{old_name.title()}* has no inventory entries.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"✅ Renamed *{old_name.title()}* → *{new_name.title()}* "
            f"({affected} item{'s' if affected != 1 else ''} updated).",
            parse_mode="Markdown",
        )


@ic_only
async def cmd_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /transfer tape bag from eunice to ally
    text  = " ".join(context.args).lower()
    match = re.match(r"(.+?)\s+from\s+(\w+)\s+to\s+(\w+)", text)
    if not match:
        await update.message.reply_text(
            "Usage: `/transfer [item] from [name] to [name]`\n"
            "Example: `/transfer tape bag from eunice to ally`",
            parse_mode="Markdown",
        )
        return
    item, from_h, to_h = match.group(1).strip(), match.group(2), match.group(3)
    if db.transfer_item(item, from_h, to_h):
        await update.message.reply_text(
            f"🔄 *{item.title()}* transferred from *{from_h.title()}* → *{to_h.title()}*.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"❌ *{from_h.title()}* doesn't have *{item}* in inventory.",
            parse_mode="Markdown",
        )


def _parse_holdings_manual(body: str) -> tuple[list[tuple[str, str, int]], list[str]]:
    """
    Loose line-by-line parser. Returns ([(name, item, qty), ...], [error_strings]).

    Item-first  (has ' - '):  "balls x 11 - michelle, saan"
                               "cones/marker discs - seraphina"
                               "bibs/tennis balls - kai"
    Person-first (no ' - '): "ella 4 balls, rena bibs"
    """
    results, errors = [], []

    for raw_line in body.splitlines():
        line = raw_line.strip().rstrip(",")
        if not line:
            continue

        if " - " in line:
            item_part, _, people_part = line.partition(" - ")

            # Multiple items separated by "/"
            raw_items = [i.strip() for i in item_part.split("/") if i.strip()]
            clean_items = []
            for it in raw_items:
                it = re.sub(r"\s*x\s*\d+\s*$", "", it, flags=re.IGNORECASE).strip()  # strip "x 11" suffix
                it = re.sub(r"^\d+\s+", "", it).strip()                               # strip leading qty
                if it:
                    clean_items.append(it.lower())

            # People: comma-separated, optional "(N)" per-person qty
            people = []
            for p in people_part.split(","):
                p = p.strip()
                if not p:
                    continue
                m = re.match(r"^(.+?)\s*\((\d+)\)\s*$", p)
                if m:
                    people.append((m.group(1).strip().lower(), int(m.group(2))))
                else:
                    people.append((p.lower(), 1))

            if not clean_items or not people:
                errors.append(f"• Couldn't parse: `{line}`")
                continue

            for person, qty in people:
                for item in clean_items:
                    results.append((person, item, qty))

        else:
            # Person-first, comma-separated segments on the same line
            for seg in line.split(","):
                seg = seg.strip()
                if not seg:
                    continue
                name, qty, item = parse_name_qty_item(seg.split())
                if name and item:
                    results.append((name.lower(), item.lower(), qty))
                else:
                    errors.append(f"• Couldn't parse: `{seg}`")

    return results, errors


@ic_only
async def cmd_update(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """
    Bulk post-training inventory update. Accepts any format:
      /update ella 4 balls, rena bibs          (person-first, inline)
      /update                                  (followed by multiline text — routed to AI)
      /update balls x11 - michelle, saan       (item-first — routed to AI)
    """
    # Extract everything after the /update command word
    full_text = (update.message.text or "").strip()
    body = re.sub(r'^/update\S*\s*', '', full_text, count=1, flags=re.IGNORECASE).strip()

    if not body:
        await update.message.reply_text(
            "Send holdings in any format after `/update`:\n\n"
            "*Person-first:* `ella 4 balls, rena bibs`\n"
            "*Item-first:* `balls x11 - michelle, saan`\n"
            "*Multiline* works too — just put each item on its own line.",
            parse_mode="Markdown",
        )
        return

    # Try Groq first (handles both formats + multiline)
    if groq_client and _check_groq_rate_limit(update.effective_user.id):
        try:
            entries = _call_groq(body)
            if entries is not None:
                if not entries:
                    await update.message.reply_text("❌ Couldn't find any holdings in that message.")
                    return
                by_holder = _apply_holdings(entries)
                lines = ["✅ *Inventory updated:*\n"]
                for name, items in sorted(by_holder.items()):
                    lines.append(f"*{name}*")
                    for item in items:
                        lines.append(f"  • {item}")
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
                return
        except Exception as e:
            logger.error("Groq parse error in /update: %s", e)
            # Fall through to manual parser

    # Manual fallback: loose line-by-line parser (item-first + person-first)
    entries, errors = _parse_holdings_manual(body)
    if not entries:
        await update.message.reply_text(
            "❌ Couldn't parse that format.\n"
            "Try: `ella 4 balls, rena bibs` or `balls x11 - michelle, saan`",
            parse_mode="Markdown",
        )
        return

    by_holder = _apply_holdings([{"name": n, "item": i, "quantity": q} for n, i, q in entries])
    lines = ["✅ *Inventory updated:*\n"]
    for name, items in sorted(by_holder.items()):
        lines.append(f"*{name}*")
        for item in items:
            lines.append(f"  • {item}")
    if errors:
        lines += ["\n⚠️ *Couldn't parse:*"] + errors
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ──────────────────────────────────────────────────────────────
# IC — TRAINING WORKFLOW
# ──────────────────────────────────────────────────────────────

@ic_only
async def cmd_training(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: `/training [DD/MM/YYYY] [venue] [time]`\n"
            "Example: `/training 11/02/2026 jurong 7:30pm`",
            parse_mode="Markdown",
        )
        return
    date, venue = context.args[0], context.args[1]
    time        = " ".join(context.args[2:])
    tid         = db.create_training(date, venue, time)
    await update.message.reply_text(
        f"📅 *Training created (#{tid})*\n"
        f"• Date: {date}\n"
        f"• Venue: {venue.upper()}\n"
        f"• Time: {time}\n\n"
        f"*Next steps:*\n"
        f"1. Reply to the attendance message with `/attendance`\n"
        f"2. Set what's needed: `/required 10 balls, bibs, ...`\n"
        f"3. Generate plan: `/delegate`",
        parse_mode="Markdown",
    )


@ic_only
async def cmd_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    training = db.get_active_training()
    if not training:
        await update.message.reply_text(
            "❌ No active training. Create one first with `/training`.",
            parse_mode="Markdown",
        )
        return

    # Priority: reply-to message > inline args
    if update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""
    elif context.args:
        text = " ".join(context.args)
    else:
        await update.message.reply_text(
            "Reply to the attendance message with `/attendance`\n\n"
            "Or type names directly:\n"
            "`/attendance Ally, Eunice, Ruhan (late 9pm)`",
            parse_mode="Markdown",
        )
        return

    attendees = parse_attendance_text(text)
    if not attendees:
        await update.message.reply_text(
            "❌ Couldn't parse any names from that message.\n"
            "Make sure each name is on its own line, or separated by commas."
        )
        return

    db.set_attendance(training["id"], attendees)

    present = [n.title() for n, s, _ in attendees if s == "present"]
    late    = [(n.title(), t) for n, s, t in attendees if s == "late"]

    lines = [f"✅ *Attendance set for {training['date']}*\n"]
    if present:
        lines.append(f"*Coming ({len(present)}):*")
        lines.append(", ".join(present))
    if late:
        lines.append(f"\n*Late:*")
        for n, t in late:
            lines.append(f"• {n} (arriving {t})")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@ic_only
async def cmd_required(update: Update, context: ContextTypes.DEFAULT_TYPE):
    training = db.get_active_training()
    if not training:
        await update.message.reply_text(
            "❌ No active training. Create one with `/training` first.",
            parse_mode="Markdown",
        )
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: `/required [items, ...]`\n"
            "Example: `/required 10 balls, bibs, bands, tape bag, marker discs`",
            parse_mode="Markdown",
        )
        return

    items = parse_items_list(" ".join(context.args))
    if not items:
        await update.message.reply_text("❌ Couldn't parse any items.")
        return

    db.set_required_items(training["id"], items)

    lines = [f"✅ *Required for {training['date']} ({training['venue']}):*\n"]
    for item, qty in items:
        lines.append(f"• {fmt(item, qty)}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@ic_only
async def cmd_delegate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Core delegation engine.
    Cross-references required items × inventory × attendance to produce:
      - Who brings what directly
      - What passes need to happen (absent holder → attending person)
      - What's missing entirely (check locker)
    
    Also outputs a ready-to-copy message for the group chat.
    """
    training = db.get_active_training()
    if not training:
        await update.message.reply_text("❌ No active training.")
        return

    required   = db.get_required_items(training["id"])
    attendance = db.get_attendance_rows(training["id"])

    if not required:
        await update.message.reply_text("❌ No required items. Use `/required` first.", parse_mode="Markdown")
        return
    if not attendance:
        await update.message.reply_text("❌ No attendance. Use `/attendance` first.", parse_mode="Markdown")
        return

    attending = {r["name"] for r in attendance if r["status"] != "absent"}

    # Build inventory map: item → [(holder, qty)]
    inv_map: dict[str, list[tuple[str, int]]] = {}
    for r in db.get_full_inventory():
        inv_map.setdefault(r["item"], []).append((r["holder"], r["quantity"]))

    bringing: list[tuple[str, str, int]] = []    # (holder, item, qty)
    passes:   list[tuple[str, str, str, int]] = [] # (from, to, item, qty)
    missing:  list[tuple[str, int]] = []           # (item, qty_shortfall)

    for req in required:
        req_item = req["item"]
        req_qty  = req["quantity"]

        holders = inv_map.get(req_item, [])
        if not holders:
            missing.append((req_item, req_qty))
            continue

        attending_holders = [(h, q) for h, q in holders if h in attending]
        absent_holders    = [(h, q) for h, q in holders if h not in attending]
        covered           = sum(q for _, q in attending_holders)

        # Every attending holder brings what they have
        for holder, qty in attending_holders:
            bringing.append((holder, req_item, qty))

        # Work out if we still need more (shortfall)
        remaining = req_qty - covered
        if remaining > 0:
            for holder, qty in absent_holders:
                if remaining <= 0:
                    break
                take = min(qty, remaining)
                # Pick a receiver: prefer someone already bringing this item
                receiver = next(
                    (b[0] for b in bringing if b[1] == req_item),
                    next(iter(sorted(attending)), None),
                )
                if receiver:
                    passes.append((holder, receiver, req_item, take))
                    remaining -= take
            if remaining > 0:
                missing.append((req_item, remaining))

    # ── Group bringing list by holder ──────────────────────────
    by_holder: dict[str, list[str]] = {}
    for holder, item, qty in bringing:
        by_holder.setdefault(holder.title(), []).append(fmt(item, qty))

    # ── Internal delegation plan (detailed) ────────────────────
    plan_lines = [
        "📋 *Delegation Plan*",
        f"📅 {training['date']} · {training['venue']} · {training['report_time']}",
        "",
    ]

    if by_holder:
        plan_lines.append("🟢 *Bringing directly:*")
        for name, items in sorted(by_holder.items()):
            plan_lines.append(f"• {name} → {', '.join(items)}")
        plan_lines.append("")

    if passes:
        plan_lines.append("🔄 *Passes needed before training:*")
        for from_h, to_h, item, qty in passes:
            plan_lines.append(
                f"• {from_h.title()} → pass {fmt(item, qty)} to {to_h.title()}"
            )
        plan_lines.append("")

    if missing:
        plan_lines.append("❓ *Not found / shortfall:*")
        for item, qty in missing:
            plan_lines.append(f"• {fmt(item, qty)} — check MPSH locker or confirm holder")
        plan_lines.append("")

    if not passes and not missing:
        plan_lines.append("✅ All items covered, no passes needed!")

    # ── Ready-to-send group message ────────────────────────────
    plan_lines += [
        "─────────────────────",
        "📤 *Copy-paste for group:*\n",
    ]

    group = [
        f"Hey team! Equipment plan for {training['date']} at "
        f"{training['venue']} ({training['report_time']}):\n"
    ]
    if by_holder:
        group.append("Please bring:")
        for name, items in sorted(by_holder.items()):
            group.append(f"• {name} — {', '.join(items)}")
    if passes:
        group.append("\nPasses needed before training:")
        for from_h, to_h, item, qty in passes:
            group.append(
                f"• {from_h.title()}, please pass {fmt(item, qty)} to {to_h.title()} ✅"
            )
    if missing:
        group.append("\nStill checking:")
        for item, qty in missing:
            group.append(f"• {fmt(item, qty)} — will confirm shortly")

    plan_lines += group
    await update.message.reply_text("\n".join(plan_lines), parse_mode="Markdown")


# ──────────────────────────────────────────────────────────────
# IC — ADMIN / HANDOVER
# ──────────────────────────────────────────────────────────────

@ic_only
async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage: `/clear [option]`\n\n"
            "• `training` — cancel current scheduled training\n"
            "• `inventory` — wipe all equipment holdings\n"
            "• `all` — full reset (new month, clean slate)",
            parse_mode="Markdown",
        )
        return

    mode = context.args[0].lower()
    if mode not in ("training", "inventory", "all"):
        await update.message.reply_text(
            "❌ Unknown option. Use: `training`, `inventory`, or `all`",
            parse_mode="Markdown",
        )
        return

    # Inline confirmation buttons
    keyboard = [[
        InlineKeyboardButton("✅ Confirm", callback_data=f"clear_confirm_{mode}"),
        InlineKeyboardButton("❌ Cancel",  callback_data="clear_cancel"),
    ]]
    labels = {
        "training":  "cancel the current training",
        "inventory": "wipe all inventory holdings",
        "all":       "do a full reset (inventory + training)",
    }
    await update.message.reply_text(
        f"⚠️ Are you sure you want to *{labels[mode]}*?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def callback_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "clear_cancel":
        await query.edit_message_text("❌ Cancelled.")
        return

    mode = query.data.replace("clear_confirm_", "")

    if mode == "training":
        cleared = db.clear_active_training()
        await query.edit_message_text(
            "🗑️ Current training cleared." if cleared else "❌ No active training to clear."
        )
    elif mode == "inventory":
        db.clear_inventory()
        await query.edit_message_text("🗑️ Inventory cleared. All holdings reset.")
    elif mode == "all":
        db.clear_inventory()
        db.clear_active_training()
        await query.edit_message_text(
            "🗑️ *Full reset complete.*\n\n"
            "• Inventory cleared\n"
            "• Training cleared\n"
            "• IC access *unchanged* — use `/handover` to transfer IC role",
            parse_mode="Markdown",
        )


@ic_only
async def cmd_listic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db.list_auth()
    if not rows:
        await update.message.reply_text("No auth entries found.")
        return
    lines = ["👥 *Access List*\n"]
    for r in rows:
        icon = "👑" if r["role"] == "master" else "🔑"
        tag  = f"@{r['username']}" if r["username"] else f"ID: {r['user_id']}"
        lines.append(f"{icon} {tag} — {r['role']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@ic_only
async def cmd_handover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Initiate handover to a new IC.
    The target user must DM the bot and type /acceptic to confirm.
    (Telegram bots can't look up a user ID from a username alone — 
    they need to message the bot first.)
    """
    if not context.args:
        await update.message.reply_text("Usage: `/handover @username`", parse_mode="Markdown")
        return

    new_username = context.args[0].lstrip("@")
    from_user    = update.effective_user
    from_role    = db.get_role(from_user.id)

    db.set_pending_handover(new_username, from_user.id, from_role)

    await update.message.reply_text(
        f"⏳ *Handover pending for @{new_username}*\n\n"
        f"Ask them to DM this bot and type `/acceptic` to confirm.\n"
        f"Until they accept, you still have IC access.",
        parse_mode="Markdown",
    )


async def cmd_acceptic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Called by the incoming IC to confirm handover."""
    user = update.effective_user
    if not user.username:
        await update.message.reply_text(
            "❌ You need a Telegram username to accept IC access.\n"
            "Set one in Telegram Settings → Edit Profile."
        )
        return

    pending = db.get_pending_handover(user.username)
    if not pending:
        await update.message.reply_text(
            "❌ No pending handover for your username.\n"
            "Ask the current IC to run `/handover @yourusername`."
        )
        return

    # If current IC is handing over (not master), clear old ICs first
    if pending["from_role"] == "ic":
        db.clear_ics()

    db.set_ic(user.id, user.username)
    db.delete_pending_handover(user.username)

    await update.message.reply_text(
        f"✅ *You're now IC for smuHBLogs!*\n\n"
        f"Type /start to see all your commands.",
        parse_mode="Markdown",
    )


@master_only
async def cmd_removeic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/removeic @username`", parse_mode="Markdown")
        return
    username = context.args[0].lstrip("@")
    if db.remove_ic_by_username(username):
        await update.message.reply_text(
            f"✅ Removed IC access for *@{username}*.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"❌ *@{username}* not found in IC list.",
            parse_mode="Markdown",
        )


# ──────────────────────────────────────────────────────────────
# AI — FREE-TEXT HOLDINGS PARSER
# ──────────────────────────────────────────────────────────────

_HOLDINGS_PROMPT = """\
You are a parser for a handball team logistics bot.
Extract equipment holdings from this message and return who holds what item and how many.

The message may use EITHER format, or a mix:

PERSON-FIRST — person then item(s):
  "ella - balls x4, bibs"         → ella: balls(4), bibs(1)
  "ruhan - balls x3"              → ruhan: balls(3)

ITEM-FIRST — item then people (separated by " - " or nothing):
  "balls x11 - michelle, saan, denise, sera, ruhan"
      → each person holds balls(1) [total is context, not per-person qty]
  "balls x10 denisse sera (4) nydia michelle"
      → denisse(1), sera(4), nydia(1), michelle(1) of balls
  "tape bag - gianna"             → gianna: tape bag(1)
  "cones/marker discs - seraphina"→ seraphina: cones(1) AND marker discs(1)
  "bibs/tennis balls - kai"       → kai: bibs(1) AND tennis balls(1)
  "resistance bands - gianna"     → gianna: resistance bands(1)
  "cones nicole ong"              → nicole ong: cones(1)  [two-word name, no separator]

Rules:
- Lowercase ALL names and items in output
- "x N" or "xN" = quantity for that item (ignore as a total, don't divide among people)
- "(N)" immediately after a name = that specific person's quantity
- "/" between items = separate items, same holder(s)
- If no quantity given, use 1
- Split multi-item, multi-person entries into individual objects
- Total quantities like "x10" on an item-first line are context only; assign per-person qty from "(N)" annotations, else 1
- If you cannot parse anything, return []

Return ONLY a JSON array, no explanation:
[{"name": "ella", "item": "balls", "quantity": 4}, {"name": "ella", "item": "bibs", "quantity": 1}]

Message:
"""


def _parse_report_time(time_str: str) -> tuple[int, int] | None:
    """Parse a free-text time like '7:30pm', '7pm', '19:30', '1930' → (hour, minute). Returns None on failure."""
    s = time_str.strip().lower().replace(" ", "")
    # 12-hour: 7:30pm, 730pm, 7pm
    m = re.match(r'^(\d{1,2})(?::?(\d{2}))?([ap]m)$', s)
    if m:
        h, mi, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ampm == 'pm' and h != 12:
            h += 12
        elif ampm == 'am' and h == 12:
            h = 0
        return h, mi
    # 24-hour: 19:30 or 1930
    m = re.match(r'^(\d{2}):?(\d{2})$', s)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _is_after_training_time() -> bool:
    """Return True if there's a scheduled training today and we're at or past its report_time."""
    training = db.get_active_training()
    if not training:
        return False
    try:
        training_date = datetime.strptime(training["date"], "%d/%m/%Y").date()
    except ValueError:
        return False
    if training_date != date.today():
        return False
    parsed = _parse_report_time(training["report_time"] or "")
    if not parsed:
        return False
    h, mi = parsed
    now = datetime.now()
    return (now.hour, now.minute) >= (h, mi)


def _call_groq(text: str) -> list | None:
    """
    Send text to Groq and return parsed holdings list, or None on failure.
    Returns [] if Groq parsed successfully but found nothing.
    Raises on hard errors (let caller handle).
    """
    response = groq_client.chat.completions.create(
        model="llama3-8b-8192",
        messages=[{"role": "user", "content": _HOLDINGS_PROMPT + text}],
        temperature=0,
    )
    raw   = response.choices[0].message.content.strip()
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if not match:
        return None
    return json.loads(match.group())


def _apply_holdings(entries: list) -> dict[str, list[str]]:
    """Write entries to DB. Returns {DisplayName: [formatted items]} for reply."""
    by_holder: dict[str, list[str]] = {}
    for e in entries:
        name = str(e["name"]).lower().strip()
        item = str(e["item"]).lower().strip()
        qty  = int(e.get("quantity", 1))
        db.set_holding(name, item, qty)
        by_holder.setdefault(name.title(), []).append(fmt(item, qty))
    return by_holder


async def handle_text_holdings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    IC/master can send a free-text message like:
      ella - balls x4, bibs
      ruhan - balls x3
      ally - cones
    Groq parses it into structured holdings and sets them in the DB.
    """
    user = update.effective_user
    text = update.message.text.strip()

    if not db.is_ic_or_master(user.id):
        # Non-IC: only handle if training has started today
        if not _is_after_training_time():
            return
        if not groq_client:
            await update.message.reply_text("❌ AI not configured.")
            return
        if not _check_groq_rate_limit(user.id):
            await update.message.reply_text("⏳ Slow down — max 5 parses per minute.")
            return
        sender_name = (user.first_name or user.username or str(user.id)).strip()
        try:
            entries = _call_groq(f"{sender_name} - {text}")
        except Exception as e:
            logger.error("Groq parse error (non-IC): %s", e)
            await update.message.reply_text("❌ Couldn't parse that.")
            return
        if not entries:
            await update.message.reply_text("❌ Couldn't find any items in that message.")
            return
        by_holder = _apply_holdings(entries)
        lines = ["✅ *Holdings logged:*\n"]
        for items in by_holder.values():
            for item in items:
                lines.append(f"  • {item}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    # Auto-detect forwarded attendance message
    parsed = parse_attendance_forward(text)
    if parsed:
        date_str, venue, time_str, attendees = parsed
        tid = db.create_training(date_str, venue, time_str)
        lines = [
            f"📅 *Training created (#{tid})*",
            f"• Date: {date_str}",
            f"• Venue: {venue.upper()}",
            f"• Time: {time_str}",
            "",
        ]
        if attendees:
            db.set_attendance(tid, attendees)
            present = [n.title() for n, s, _ in attendees if s == "present"]
            late    = [(n.title(), t) for n, s, t in attendees if s == "late"]
            lines.append(f"✅ *Attendance set ({len(present) + len(late)} people)*")
            if present:
                lines.append(", ".join(present))
            if late:
                lines.append("\n*Late:*")
                for n, t in late:
                    lines.append(f"• {n} (arriving {t})")
        else:
            lines.append("⚠️ No attendees found in message.")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    if not groq_client:
        await update.message.reply_text("❌ GROQ_API_KEY not configured.")
        return
    if not _check_groq_rate_limit(update.effective_user.id):
        await update.message.reply_text("⏳ Slow down — max 5 AI parses per minute.")
        return

    try:
        entries = _call_groq(text)
    except Exception as e:
        logger.error("Groq parse error: %s", e)
        await update.message.reply_text("❌ AI parsing failed. Try again or use `/update`.", parse_mode="Markdown")
        return

    if entries is None:
        await update.message.reply_text("❌ Couldn't parse that.", parse_mode="Markdown")
        return
    if not entries:
        await update.message.reply_text("❌ Couldn't find any holdings in that message.")
        return

    by_holder = _apply_holdings(entries)
    lines = ["✅ *Holdings updated:*\n"]
    for name, items in sorted(by_holder.items()):
        lines.append(f"*{name}*")
        for item in items:
            lines.append(f"  • {item}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def main():
    db.init_db(MASTER_ID)
    logger.info("Database initialised. Master ID: %d", MASTER_ID)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("inventory",   cmd_inventory))
    app.add_handler(CommandHandler("whohas",      cmd_whohas))
    app.add_handler(CommandHandler("acceptic",    cmd_acceptic))

    app.add_handler(CommandHandler("setholding",  cmd_setholding))
    app.add_handler(CommandHandler("removeitem",  cmd_removeitem))
    app.add_handler(CommandHandler("rename",      cmd_rename))
    app.add_handler(CommandHandler("transfer",    cmd_transfer))
    app.add_handler(CommandHandler("update",      cmd_update))

    app.add_handler(CommandHandler("training",    cmd_training))
    app.add_handler(CommandHandler("attendance",  cmd_attendance))
    app.add_handler(CommandHandler("required",    cmd_required))
    app.add_handler(CommandHandler("delegate",    cmd_delegate))

    app.add_handler(CommandHandler("clear",       cmd_clear))
    app.add_handler(CallbackQueryHandler(callback_clear, pattern="^clear_"))
    app.add_handler(CommandHandler("listic",      cmd_listic))
    app.add_handler(CommandHandler("handover",    cmd_handover))
    app.add_handler(CommandHandler("removeic",    cmd_removeic))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_holdings))

    logger.info("smuHBLogs is running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

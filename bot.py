from __future__ import annotations
"""
bot.py — smuHBLogs Telegram Bot
Handball team logistics tracker for SMU.

Commands
─────── 
Public (anyone can DM the bot):
  /start               — welcome + command list
  /attendance                           ← pick session from sheet; view attendance
  /attendancepos                        ← attendance grouped by position (reads sheet71)
  /inventory           — see all holdings
  /inventory [item]    — who has a specific item
  /whohas [name]       — what someone is holding
  /players             — list all player names in the DB
  /acceptic            — accept a pending IC handover
  /update [name] [qty?] [item], ...     ← bulk inventory update
  /ask [question]      — ask the bot a question about commands or logistics

IC-only:
  /setholding [name] [qty?] [item]
  /removeitem [name] [item]
  /transfer [item] from [name] to [name]
  /training [DD/MM/YYYY] [venue] [time] ← optional: manually create training
  /required [items, ...]
  /delegate                             ← generate delegation plan + copy-paste message
  /alias [sheet_name] as [display_name] ← map sheet name to display name
  /unalias [sheet_name]                 ← remove a name alias
  /clear training|inventory|all
  /handover @username
  /reminderchat                         ← redirect training reminders to current chat
  /listic

Master-only:
  /removeic @username
"""

import os
import re
import json
import time
import logging
from datetime import datetime, date, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from collections import defaultdict, deque
from dotenv import load_dotenv
load_dotenv()
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
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
SHEET_ID             = os.getenv("SHEET_ID", "")
SHEET_NAME           = os.getenv("SHEET_NAME", "Sheet1")
SHEET_POSITIONS_NAME = os.getenv("SHEET_POSNAME", "sheet71")
SHEET_CREDS          = os.getenv("SHEET_CREDS", "service_account.json")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set.")

# Google Sheets integration (optional — only active when SHEET_ID is set)
_sheets_enabled = bool(SHEET_ID and SHEET_CREDS)
if _sheets_enabled:
    import sheets as _sheets
    logger.info("Google Sheets integration enabled (sheet: %s / %s)", SHEET_ID, SHEET_NAME)
else:
    _sheets = None  # type: ignore

# Polling state — tracks the last seen attendance column so we can diff on changes
_last_sheet_hash: Optional[str] = None   # None = not yet initialised
_last_sheet_data: dict = {}

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


# Map nicknames → canonical DB names to prevent double-counting.
# Add entries here whenever a short name causes a duplicate holder.
NAME_ALIASES: dict[str, str] = {
    "ally": "allison",
    "sera": "seraphina",
}


def resolve_name(name: str) -> str:
    """Return the canonical name for a nickname, or the name itself if not aliased."""
    return NAME_ALIASES.get(name.lower().strip(), name.lower().strip())


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

    lines = [f"<b>smuHBLogs</b> — {badge}\n"]

    if is_ic:
        training = db.get_active_training()

        if not training:
            lines += [
                "No upcoming training set.\n",
                "To get started:",
                "• /training DD/MM/YYYY venue time — create a session",
                "• /attendance — pick a session from Google Sheets\n",
                "📦 /inventory — check current equipment",
            ]
            keyboard = [["/inventory", "/whohas"], ["/training", "/help"]]
        else:
            attendance = db.get_attendance_rows(training["id"])
            required   = db.get_required_items(training["id"])

            lines += [
                f"📅 <b>{training['date']}</b> · {training['venue']} · {training['report_time']}\n",
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

            if not attendance:
                lines += [
                    "<b>Next step:</b> Set attendance",
                    "Run /attendance to pick a session from Google Sheets",
                ]
                keyboard = [["/attendance", "/inventory"], ["/required", "/help"]]
            elif not required:
                lines += [
                    "<b>Next step:</b> Set required items",
                    "/required 10 balls, bibs, tape bag, ...",
                ]
                keyboard = [["/required", "/delegate"], ["/inventory", "/help"]]
            else:
                lines += [
                    "<b>Ready to go!</b> Run /delegate to generate the equipment plan.",
                ]
                keyboard = [["/delegate", "/inventory"], ["/required", "/clear"], ["/help"]]

        lines.append("\n/help — all commands")
    else:
        lines += [
            "Welcome! I'm the smuHBLogs bot, here to help track handball training logistics.\n (It was too manual before)\n\nStart by checking attendance and inventory:",
            "📦 /inventory — see all equipment holdings",
            "📦 /inventory [item] — who has something specific",
            "👤 /whohas [name] — what someone is holding",
            "✅ /acceptic — accept a pending IC handover",
        ]
        keyboard = [["/attendance", "/inventory"], ["/update", "/help"]]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=reply_markup)


async def cmd_help(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    role  = db.get_role(update.effective_user.id) or "viewer"
    is_ic = role in ("ic", "master")

    lines = ["📖 <b>Commands</b>\n"]
    lines += [
        "<b>Anyone:</b>",
        "/attendance — pick from upcoming sessions (view attendance)",
        "/attendancepos — same as /attendance but grouped by position",
        "/inventory — view all equipment holdings",
        "/inventory [item] — see who has a specific item",
        "/whohas [name] — see what someone is holding",
        "/players — list all player names in the DB",
        "/acceptic — accept a pending IC handover",
        "/update [name] [qty] [item], ... — bulk inventory update",
        "/ask [question] — ask a question about commands or logistics",
    ]

    if is_ic:
        lines += [
            "",
            "<b>Training:</b>",
            "/training [DD/MM/YYYY] [venue] [time] — manually create a training session",
            "/sheetattendance [DD/MM/YYYY] — pull attendance for a specific date",
            "/required [items, ...] — set equipment needed for training",
            "/delegate — generate equipment delegation plan",
            "/reminderchat — set this chat as the auto-reminder channel",
            "",
            "<b>Inventory:</b>",
            "/setholding [name] [qty] [item] — assign item to someone",
            "/removeitem [name] [item] — remove item from someone",
            "/rename [old] to [new] — rename a holder",
            "/transfer [item] from [name] to [name] — move item between holders",
            "",
            "<b>Admin:</b>",
            "/alias [sheet_name] as [display_name] — map a sheet name to a display name",
            "/unalias [sheet_name] — remove a name alias",
            "/clear training|inventory|all — wipe data",
            "/handover @username — hand over IC role",
            "/listic — list who has IC/master access",
        ]
        if role == "master":
            lines.append("/removeic @username — revoke IC access")

    lines += ["", "💬 <i>Got a question? /ask [question]</i>"]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


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
            lines.append(f"• {resolve_name(r['holder']).title()} — {fmt(r['item'], r['quantity'])}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    rows = db.get_full_inventory()
    if not rows:
        is_ic = db.is_ic_or_master(update.effective_user.id)
        hint  = "Use `/setholding` or `/update` to log who has what." if is_ic else "Use `/update` to log who has what."
        await update.message.reply_text(f"📭 Inventory is empty.\n{hint}", parse_mode="Markdown")
        return

    # Group items by holder for a cleaner display
    holders: dict[str, list[str]] = {}
    for r in rows:
        holders.setdefault(resolve_name(r["holder"]).title(), []).append(fmt(r["item"], r["quantity"]))

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
    name = resolve_name(" ".join(context.args))
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


async def cmd_players(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """List all player names currently in the inventory DB."""
    holders = db.get_all_holders()
    if not holders:
        is_ic = db.is_ic_or_master(update.effective_user.id)
        hint  = "Use `/update` or `/setholding` to log inventory." if is_ic else "Use `/update` to log inventory."
        await update.message.reply_text(f"📭 No players in the DB yet.\n{hint}", parse_mode="Markdown")
        return
    lines = [f"👥 *Players in DB ({len(holders)}):*\n"]
    for h in holders:
        lines.append(f"• {resolve_name(h).title()}")
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
    raw_name, qty, item = parse_name_qty_item(context.args)
    name = resolve_name(raw_name)
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
    holder = resolve_name(context.args[0])
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
    item  = match.group(1).strip()
    from_h = resolve_name(match.group(2))
    to_h   = resolve_name(match.group(3))
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
            "Who has what? Reply with the holdings below 👇\n\n"
            "Format options:\n"
            "• `rena bibs, ella 4 balls` — person-first, comma = new person\n"
            "• `rena - bibs, tennis balls` — person-first, comma = new item\n"
            "• `balls x11 - michelle, saan` — item-first\n"
            "• Multiline works too, one entry per line",
            parse_mode="Markdown",
        )
        return

    # Try Groq first (handles both formats + multiline)
    if groq_client and _check_groq_rate_limit(update.effective_user.id):
        try:
            entries = _call_groq(body)
            if entries:
                by_holder = _apply_holdings(entries)
                lines = ["✅ *Inventory updated:*\n"]
                for name, items in sorted(by_holder.items()):
                    lines.append(f"*{name}*")
                    for item in items:
                        lines.append(f"  • {item}")
                await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
                return
            # entries is None (Groq failed) or [] (Groq found nothing) — fall through to manual parser
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
    date_str, venue = context.args[0], context.args[1]
    time_str        = " ".join(context.args[2:])
    chat_id         = update.effective_chat.id
    tid             = db.create_training(date_str, venue, time_str, reminder_chat_id=chat_id)

    n = _schedule_training_reminders(context.application, tid, date_str, chat_id)
    reminder_note = (
        "\n\n🔔 *Reminder set* — I'll ping you the day before to check inventory & requirements."
        if n > 0 else
        "\n\n⚠️ No reminder scheduled (training may be tomorrow or already past)."
    )

    await update.message.reply_text(
        f"📅 *Training created (#{tid})*\n"
        f"• Date: {date_str}\n"
        f"• Venue: {venue.upper()}\n"
        f"• Time: {time_str}\n\n"
        f"*Next steps:*\n"
        f"1. Reply to the attendance message with `/attendance`\n"
        f"2. Set what's needed: `/required 10 balls, bibs, ...`\n"
        f"3. Generate plan: `/delegate`"
        f"{reminder_note}\n\n"
        f"_Use /reminderchat in a group to redirect reminders there instead._",
        parse_mode="Markdown",
    )


def _build_attendance_msgs(sheet_data: dict, training) -> tuple[str, Optional[str]]:
    """
    Build the plain-text attendance message and (optionally) the equipment plan.

    Returns:
      (attendance_msg, plan_msg)   — plan_msg is None if venue is VR or no required items.
    Also saves attendance to DB as a side-effect.
    """
    training_date = sheet_data["date"]
    venue    = (training.get("venue") if training else None) or sheet_data.get("venue") or "TBC"
    time_str = (training.get("report_time") if training else None) or sheet_data.get("time") or "TBC"

    coming       = []
    db_attendees = []
    for name, parsed in sheet_data["attendance"].items():
        display = resolve_name(name).title()
        canon   = resolve_name(name)
        s = parsed.get("status")
        if s == "present":
            coming.append(display)
            db_attendees.append((canon, "present", None))
        elif s == "late":
            parts = ["late"]
            if parsed.get("reason"):
                parts.append(parsed["reason"])
            coming.append(f"{display} ({', '.join(parts)})")
            db_attendees.append((canon, "late", parsed.get("eta")))

    if training and db_attendees:
        db.set_attendance(training["id"], db_attendees)

    date_str = training_date.strftime("%d/%m/%y")
    att_msg  = "\n".join(
        [f"Attendance {date_str}", ""] + coming + ["", f"Location: {venue}", f"Time: {time_str}"]
    )

    # No equipment plan for VR
    if venue.upper().startswith("VR"):
        return att_msg, None

    required = db.get_required_items(training["id"]) if training else []
    if not required:
        return att_msg, None

    attending: set[str] = set()
    for _n, _p in sheet_data["attendance"].items():
        if _p.get("status") not in ("present", "late"):
            continue
        _lower = _n.lower().strip()
        _first = _lower.split()[0]
        attending.add(_lower)
        attending.add(_first)
        attending.add(resolve_name(_lower))
        attending.add(resolve_name(_first))

    inv_map: dict[str, list[tuple[str, int]]] = {}
    for r in db.get_full_inventory():
        inv_map.setdefault(r["item"], []).append((r["holder"], r["quantity"]))

    bringing: list[tuple[str, str, int]] = []
    passes:   list[tuple[str, str, str, int]] = []
    missing:  list[tuple[str, int]] = []

    for req in required:
        req_item = req["item"]
        req_qty  = req["quantity"]
        holders  = inv_map.get(req_item, [])
        if not holders:
            missing.append((req_item, req_qty))
            continue
        attending_holders = [(h, q) for h, q in holders if h in attending]
        absent_holders    = [(h, q) for h, q in holders if h not in attending]
        covered           = sum(q for _, q in attending_holders)
        for holder, qty in attending_holders:
            bringing.append((holder, req_item, qty))
        remaining = req_qty - covered
        if remaining > 0:
            for holder, qty in absent_holders:
                if remaining <= 0:
                    break
                take     = min(qty, remaining)
                receiver = next(
                    (b[0] for b in bringing if b[1] == req_item),
                    next(iter(sorted(attending)), None),
                )
                if receiver:
                    passes.append((holder, receiver, req_item, take))
                    remaining -= take
            if remaining > 0:
                missing.append((req_item, remaining))

    by_holder: dict[str, list[str]] = {}
    for holder, item, qty in bringing:
        by_holder.setdefault(resolve_name(holder).title(), []).append(fmt(item, qty))

    plan_lines = [f"📋 *Equipment Plan — {date_str} · {venue} · {time_str}*\n"]
    if by_holder:
        plan_lines.append("🟢 *Bringing directly:*")
        for name, items in sorted(by_holder.items()):
            plan_lines.append(f"• {name} → {', '.join(items)}")
        plan_lines.append("")
    if passes:
        plan_lines.append("🔄 *Passes needed:*")
        for from_h, to_h, item, qty in passes:
            plan_lines.append(f"• {resolve_name(from_h).title()} → pass {fmt(item, qty)} to {resolve_name(to_h).title()}")
        plan_lines.append("")
    if missing:
        plan_lines.append("❓ *Not found / shortfall:*")
        for item, qty in missing:
            plan_lines.append(f"• {fmt(item, qty)} — check locker")
        plan_lines.append("")
    if not passes and not missing:
        plan_lines.append("✅ All items covered, no passes needed!")

    plan_lines += ["─────────────────────", "📤 *Copy-paste for group:*\n"]
    group = [f"Hey team! Equipment plan for {date_str} at {venue} ({time_str}):\n"]
    if by_holder:
        group.append("Please bring:")
        for name, items in sorted(by_holder.items()):
            group.append(f"• {name} — {', '.join(items)}")
    if passes:
        group.append("\nPasses needed before training:")
        for from_h, to_h, item, qty in passes:
            group.append(f"• {resolve_name(from_h).title()}, please pass {fmt(item, qty)} to {resolve_name(to_h).title()} ✅")
    if missing:
        group.append("\nStill checking:")
        for item, qty in missing:
            group.append(f"• {fmt(item, qty)} — will confirm shortly")
    plan_lines += group

    return att_msg, "\n".join(plan_lines)


async def cmd_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    training = db.get_active_training()

    # Priority: reply-to message > inline args > generate from sheet
    if update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""
    elif context.args:
        text = " ".join(context.args)
    else:
        # No-arg path: show the next 3 upcoming training sessions as buttons (sheet-based)
        if not _sheets_enabled:
            await update.message.reply_text(
                "Reply to the attendance message with `/attendance`\n\n"
                "Or type names directly:\n"
                "`/attendance Ally, Eunice, Ruhan (late 9pm)`",
                parse_mode="Markdown",
            )
            return

        try:
            sessions = _sheets.get_upcoming_sessions(SHEET_ID, SHEET_NAME, SHEET_CREDS, limit=3)
        except Exception as e:
            logger.error("Sheet session fetch error: %s", e)
            await update.message.reply_text(f"❌ Couldn't read sheet: {e}")
            return

        if not sessions:
            await update.message.reply_text("❌ No upcoming training sessions found in the sheet.")
            return

        keyboard = []
        for s in sessions:
            label         = s["date"].strftime("%-d %b") + f"  ·  {s['venue']}  ·  {s['time']}"
            callback_data = f"att_pick_{s['date'].strftime('%d%m%Y')}"
            keyboard.append([InlineKeyboardButton(label, callback_data=callback_data)])

        await update.message.reply_text(
            "Which training do you want the attendance list for?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if not training:
        await update.message.reply_text(
            "❌ No active training found.\n"
            "Use `/attendance` without arguments to pick a session from the sheet.",
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


async def callback_attendance_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle training date selection from /attendance inline keyboard."""
    query = update.callback_query
    await query.answer()

    date_str = query.data.replace("att_pick_", "")  # DDMMYYYY
    try:
        target_date = datetime.strptime(date_str, "%d%m%Y").date()
    except ValueError:
        await query.edit_message_text("❌ Invalid date.")
        return

    await query.edit_message_text(f"⏳ Fetching sheet for {target_date.strftime('%-d %b %Y')}…")

    try:
        sheet_data = _sheets.get_attendance(SHEET_ID, SHEET_NAME, SHEET_CREDS, target_date)
    except Exception as e:
        logger.error("Sheet fetch error in att_pick: %s", e)
        await query.edit_message_text(f"❌ Couldn't read sheet: {e}")
        return

    if sheet_data is None:
        await query.edit_message_text(
            f"❌ No column for {target_date.strftime('%-d %b %Y')} found in the sheet."
        )
        return

    # Find or auto-create a training record for this date
    try:
        date_for_db = target_date.strftime("%d/%m/%Y")
        matched_training = db.get_training_by_date(date_for_db)
        if matched_training:
            matched_training = dict(matched_training)
        if not matched_training:
            venue    = sheet_data.get("venue") or "TBC"
            time_str = sheet_data.get("time") or "TBC"
            chat_id  = query.message.chat_id
            tid = db.create_training(date_for_db, venue, time_str, reminder_chat_id=chat_id)
            _schedule_training_reminders(context.application, tid, date_for_db, chat_id)
            row = db.get_training_by_date(date_for_db)
            matched_training = dict(row) if row else None

        att_msg, plan_msg = _build_attendance_msgs(sheet_data, matched_training)

        if not any(
            p.get("status") in ("present", "late")
            for p in sheet_data["attendance"].values()
        ):
            await query.edit_message_text("❌ Nobody is marked as coming in the sheet yet.")
            return

        await query.edit_message_text(att_msg)
        if plan_msg:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=plan_msg,
                parse_mode="Markdown",
            )
        elif matched_training and not matched_training.get("venue", "").upper().startswith("VR"):
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=(
                    "ℹ️ *Attendance saved.* No required items set yet.\n"
                    "Run `/required 10 balls, bibs, ...` then `/delegate` for the equipment plan."
                ),
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.error("Error in callback_attendance_pick: %s", e, exc_info=True)
        await query.edit_message_text(f"❌ Something went wrong: {e}")


# Position grouping for /attendancepos
_POS_ORDER = ["Goalkeeper", "Pivot", "Back", "Wing"]
_POS_LABELS = {
    "Goalkeeper": "Keeper",
    "Pivot":      "Pivots",
    "Back":       "CBs",
    "Wing":       "Wings",
}


def _build_attendancepos_msg(sheet_data: dict, positions: dict) -> str:
    """Build a position-grouped attendance message from sheet data and positions roster."""
    training_date = sheet_data["date"]
    venue    = sheet_data.get("venue") or "TBC"
    time_str = sheet_data.get("time")  or "TBC"
    date_str = training_date.strftime("%d/%m/%y")

    attendance = sheet_data["attendance"]

    # Group attending players by position
    groups: dict[str, list[str]] = {pos: [] for pos in _POS_ORDER}
    unknown: list[str] = []
    for name, parsed in attendance.items():
        if parsed.get("status") not in ("present", "late"):
            continue
        if parsed.get("status") == "late":
            parts = ["late"]
            if parsed.get("reason"):
                parts.append(parsed["reason"])
            display = f"{resolve_name(name)} ({', '.join(parts)})"
        else:
            display = resolve_name(name)
        pos = positions.get(name, "") or positions.get(resolve_name(name), "")
        if pos in groups:
            groups[pos].append(display)
        else:
            unknown.append(display)

    lines = [f"Attendance {date_str}", ""]
    for pos in _POS_ORDER:
        members = groups[pos]
        if not members:
            continue
        label = _POS_LABELS[pos]
        lines.append(f"{label} ({len(members)})")
        for m in members:
            lines.append(m)
        lines.append("")

    if unknown:
        lines.append(f"Others ({len(unknown)})")
        for m in unknown:
            lines.append(m)
        lines.append("")

    lines += [f"Location: {venue}", f"Time: {time_str}"]
    return "\n".join(lines)


async def cmd_attendancepos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show attendance grouped by position, fetched from Google Sheets."""
    if not _sheets_enabled:
        await update.message.reply_text("❌ Google Sheets integration is not enabled.")
        return

    try:
        sessions = _sheets.get_upcoming_sessions(SHEET_ID, SHEET_NAME, SHEET_CREDS, limit=3)
    except Exception as e:
        logger.error("Sheet session fetch error: %s", e)
        await update.message.reply_text(f"❌ Couldn't read sheet: {e}")
        return

    if not sessions:
        await update.message.reply_text("❌ No upcoming training sessions found in the sheet.")
        return

    keyboard = []
    for s in sessions:
        label         = s["date"].strftime("%-d %b") + f"  ·  {s['venue']}  ·  {s['time']}"
        callback_data = f"attpos_pick_{s['date'].strftime('%d%m%Y')}"
        keyboard.append([InlineKeyboardButton(label, callback_data=callback_data)])

    await update.message.reply_text(
        "Which training do you want the position attendance for?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def callback_attpos_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle training date selection from /attendancepos inline keyboard."""
    query = update.callback_query
    await query.answer()

    date_str = query.data.replace("attpos_pick_", "")  # DDMMYYYY
    try:
        target_date = datetime.strptime(date_str, "%d%m%Y").date()
    except ValueError:
        await query.edit_message_text("❌ Invalid date.")
        return

    await query.edit_message_text(f"⏳ Fetching sheet for {target_date.strftime('%-d %b %Y')}…")

    try:
        sheet_data = _sheets.get_attendance(SHEET_ID, SHEET_NAME, SHEET_CREDS, target_date)
    except Exception as e:
        logger.error("Sheet fetch error in attpos_pick: %s", e)
        await query.edit_message_text(f"❌ Couldn't read sheet: {e}")
        return

    if sheet_data is None:
        await query.edit_message_text(
            f"❌ No column for {target_date.strftime('%-d %b %Y')} found in the sheet."
        )
        return

    try:
        if not any(
            p.get("status") in ("present", "late")
            for p in sheet_data["attendance"].values()
        ):
            await query.edit_message_text("❌ Nobody is marked as coming in the sheet yet.")
            return

        try:
            positions = _sheets.get_positions(SHEET_ID, SHEET_POSITIONS_NAME, SHEET_CREDS)
        except Exception as e:
            logger.error("Position sheet fetch error in attpos_pick: %s", e)
            positions = {}

        msg = _build_attendancepos_msg(sheet_data, positions)
        await query.edit_message_text(msg)
    except Exception as e:
        logger.error("Error in callback_attpos_pick: %s", e, exc_info=True)
        await query.edit_message_text(f"❌ Something went wrong: {e}")


async def _show_required_session_picker(reply_fn):
    """Fetch upcoming sessions from the sheet and show as inline buttons."""
    try:
        sessions = _sheets.get_upcoming_sessions(SHEET_ID, SHEET_NAME, SHEET_CREDS, limit=3)
    except Exception as e:
        logger.error("Sheet session fetch error: %s", e)
        await reply_fn(f"❌ Couldn't read sheet: {e}")
        return

    if not sessions:
        await reply_fn("❌ No upcoming training sessions found in the sheet.")
        return

    keyboard = []
    for s in sessions:
        label         = s["date"].strftime("%-d %b") + f"  ·  {s['venue']}  ·  {s['time']}"
        callback_data = f"req_pick_{s['date'].strftime('%d%m%Y')}"
        keyboard.append([InlineKeyboardButton(label, callback_data=callback_data)])

    await reply_fn(
        "Which training do you want to set required items for?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@ic_only
async def cmd_required(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _sheets_enabled:
        # No sheets: require items upfront and apply to active training
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
        training = db.get_active_training()
        if not training:
            await update.message.reply_text(
                "❌ No active training. Create one with `/training` first.",
                parse_mode="Markdown",
            )
            return
        db.set_required_items(training["id"], items)
        lines = [f"✅ *Required for {training['date']} ({training['venue']}):*\n"]
        for item, qty in items:
            lines.append(f"• {fmt(item, qty)}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    await _show_required_session_picker(update.message.reply_text)


async def callback_required_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle training date selection from /required inline keyboard."""
    query = update.callback_query
    await query.answer()

    if not db.is_ic_or_master(query.from_user.id):
        await query.edit_message_text("🔒 IC or master access required.")
        return

    date_str = query.data.replace("req_pick_", "")  # DDMMYYYY
    try:
        target_date = datetime.strptime(date_str, "%d%m%Y").date()
    except ValueError:
        await query.edit_message_text("❌ Invalid date.")
        return

    date_for_db = target_date.strftime("%d/%m/%Y")
    matched_training = db.get_training_by_date(date_for_db)
    if matched_training:
        matched_training = dict(matched_training)

    if not matched_training:
        await query.edit_message_text(
            f"❌ No training record for {target_date.strftime('%-d %b %Y')}. "
            "Run `/attendance` first to register the session.",
            parse_mode="Markdown",
        )
        return

    context.user_data["req_training"] = dict(matched_training)
    await query.edit_message_text(
        f"✅ *{matched_training['date']} ({matched_training['venue']})* selected.\n\n"
        "Please enter the required items (e.g. `10 balls, bibs, tape bag`).",
        parse_mode="Markdown",
    )


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

    # Normalise attendance names: keep both full name and first token so that
    # "szehan binte" in the sheet still matches inventory holder "szehan".
    attending_raw = {r["name"] for r in attendance if r["status"] != "absent"}
    attending: set[str] = set()
    for n in attending_raw:
        lower = n.lower().strip()
        first = lower.split()[0]
        attending.add(lower)
        attending.add(first)
        attending.add(resolve_name(lower))   # alias of full name
        attending.add(resolve_name(first))   # alias of first name

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
        by_holder.setdefault(resolve_name(holder).title(), []).append(fmt(item, qty))

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
                f"• {resolve_name(from_h).title()} → pass {fmt(item, qty)} to {resolve_name(to_h).title()}"
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
                f"• {resolve_name(from_h).title()}, please pass {fmt(item, qty)} to {resolve_name(to_h).title()} ✅"
            )
    if missing:
        group.append("\nStill checking:")
        for item, qty in missing:
            group.append(f"• {fmt(item, qty)} — will confirm shortly")

    plan_lines += group
    await update.message.reply_text("\n".join(plan_lines), parse_mode="Markdown")


# ──────────────────────────────────────────────────────────────
# IC — NAME ALIASES
# ──────────────────────────────────────────────────────────────

@ic_only
async def cmd_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /alias [sheet_name] as [display_name]  — map a sheet name to a display name
    /alias                                 — list all active aliases
    """
    if not context.args:
        aliases = db.get_all_name_aliases()
        if not aliases:
            await update.message.reply_text("ℹ️ No aliases set yet.\n\nUsage: `/alias szehan as saan`", parse_mode="Markdown")
            return
        lines = ["📋 *Name aliases (sheet → display):*\n"]
        for sheet, display in sorted(aliases.items()):
            lines.append(f"• `{sheet}` → `{display}`")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    raw = " ".join(context.args)
    m = re.match(r'^(\S+)\s+as\s+(\S+)$', raw, re.IGNORECASE)
    if not m:
        await update.message.reply_text(
            "Usage: `/alias [sheet_name] as [display_name]`\nExample: `/alias szehan as saan`",
            parse_mode="Markdown",
        )
        return

    sheet_name   = m.group(1).lower().strip()
    display_name = m.group(2).lower().strip()
    db.set_name_alias(sheet_name, display_name)
    NAME_ALIASES[sheet_name] = display_name
    await update.message.reply_text(
        f"✅ Alias saved: `{sheet_name}` → `{display_name}`\n\n"
        f"Sheet entries named *{sheet_name}* will now appear as *{display_name.title()}* in messages. "
        f"Both names are accepted in commands.",
        parse_mode="Markdown",
    )


@ic_only
async def cmd_unalias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /unalias [sheet_name]  — remove an alias
    """
    if not context.args:
        await update.message.reply_text("Usage: `/unalias [sheet_name]`", parse_mode="Markdown")
        return
    sheet_name = context.args[0].lower().strip()
    if db.remove_name_alias(sheet_name):
        NAME_ALIASES.pop(sheet_name, None)
        await update.message.reply_text(f"✅ Alias for `{sheet_name}` removed.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ No alias found for `{sheet_name}`.", parse_mode="Markdown")


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
async def cmd_reminderchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Set the current chat as the destination for training reminders.
    Run this from a group chat so reminders go there instead of the IC's DM.
    """
    training = db.get_active_training()
    if not training:
        await update.message.reply_text(
            "❌ No active training. Create one with `/training` first.",
            parse_mode="Markdown",
        )
        return

    chat_id = update.effective_chat.id
    db.set_training_reminder_chat(training["id"], chat_id)

    n = _schedule_training_reminders(context.application, training["id"], training["date"], chat_id)
    await update.message.reply_text(
        f"🔔 *Reminders redirected to this chat!*\n"
        f"{n} reminder(s) rescheduled for training on {training['date']}.",
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
# AI — HELP ASSISTANT
# ──────────────────────────────────────────────────────────────

_HELP_SYSTEM_PROMPT = """\
You are a concise assistant for a Telegram handball logistics bot (smuHBLogs).

Your ONLY purpose is to help users understand and use bot commands for team logistics.

----------------------------------------
SCOPE RULES
----------------------------------------
You may ONLY:
- Explain bot commands
- Help users choose the correct command
- Clarify logistics workflows (equipment, attendance, delegation, handover)
- Reformat messy user input into commands

If a message is unrelated to bot usage or team logistics, reply EXACTLY:
"I can only help with bot commands and team logistics. Try /help for the full list."

----------------------------------------
BEHAVIOUR RULES
----------------------------------------
- Be concise. Max 1–3 short sentences unless listing commands
- Do NOT explain internal logic, database, or system design
- Do NOT guess missing information — ask a short clarifying question instead
- Do NOT invent commands
- Only use commands from the list below
- If user intent is unclear → suggest closest valid command

----------------------------------------
COMMAND RULES
----------------------------------------

Commands (anyone):
/attendance
/attendancepos
/inventory [item]
/whohas [name]
/players
/acceptic
/update [name] [qty] [item], ...
/ask [question]

Commands (IC only):
/training
/sheetattendance
/required
/delegate
/reminderchat
/setholding
/removeitem
/rename
/transfer
/alias
/unalias
/clear
/handover
/listic

Commands (master only):
/removeic

----------------------------------------
RESPONSE PATTERNS
----------------------------------------

1. If user asks "what do I do":
→ Suggest ONE best command
Example:
"Use /required to set equipment needed for the training."

2. If user gives messy logistics info:
→ Convert into /update format (available to everyone)
Example:
Input: "ella has 4 balls and im bringing bands"
Output:
"Use:
/update ella 4 balls, [your name] bands"

3. If user asks about items:
→ Point to inventory commands
Example:
"Use /inventory balls or /whohas Ella"

4. If delegation-related:
→ Suggest /delegate
Example:
"Run /delegate after setting attendance and required items."

5. If attendance-related:
→ Suggest /attendance or /sheetattendance

6. If handover-related:
→ Suggest /handover or /transfer

7. If missing info:
→ Ask ONE short clarifying question
Example:
"Which training is this for?"

----------------------------------------
STYLE
----------------------------------------
- Direct, no fluff
- No emojis unless user uses them first
- No long explanations
- Prefer command-first answers

----------------------------------------
FAILSAFE
----------------------------------------
If unsure:
→ Suggest /help OR the closest matching command"""

async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/ask [your question]`\nExample: `/ask how do I transfer an item?`", parse_mode="Markdown")
        return
    if not groq_client:
        await update.message.reply_text("❌ AI not configured (GROQ_API_KEY missing).")
        return
    if not _check_groq_rate_limit(update.effective_user.id):
        await update.message.reply_text("⏳ Slow down — max 5 questions per minute.")
        return

    role   = db.get_role(update.effective_user.id) or "viewer"
    is_ic  = role in ("ic", "master")
    if role == "master":
        role_note = "USER ROLE: master — may use all commands including master-only."
    elif is_ic:
        role_note = "USER ROLE: ic — may use all commands EXCEPT master-only commands. Do NOT suggest /removeic."
    else:
        role_note = (
            "USER ROLE: viewer (not IC) — may ONLY use commands from the 'Commands (anyone)' list "
            "(includes /update). "
            "Do NOT suggest any IC-only or master-only commands. "
            "If their question requires an IC command (e.g. /setholding, /delegate, /required), "
            "tell them to ask an IC to run it instead."
        )
    system_content = role_note + "\n\n" + _HELP_SYSTEM_PROMPT

    question = " ".join(context.args)
    try:
        response = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": question},
            ],
            temperature=0,
            max_tokens=256,
        )
        answer = response.choices[0].message.content.strip()
    except Exception as e:
        logger.error("Help AI error: %s", e)
        await update.message.reply_text("❌ Couldn't get an answer. Try again.")
        return
    await update.message.reply_text(f"💬 {answer}")


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
  "rena bibs tennis balls"        → rena: bibs(1), tennis balls(1)  [space-separated distinct items]
  "rena - bibs, tennis balls"     → rena: bibs(1), tennis balls(1)  [comma-separated items after dash]
  "rena has 4 balls"              → rena: balls(4)  ["has" is a filler word, ignore it]
  "rena has balls"                → rena: balls(1)
  "rena 4 balls"                  → rena: balls(4)  [number before item = quantity]

MULTIPLE ENTRIES comma-separated on one line (each entry is person + optional qty + item):
  "rena 4 balls, ella bibs"       → rena: balls(4), ella: bibs(1)
  "rena has 4 balls, ella bibs"   → rena: balls(4), ella: bibs(1)
  "rena balls, ella 2 bibs"       → rena: balls(1), ella: bibs(2)

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
- Ignore filler words like "has", "have", "holds", "with", "got" between name and item/quantity
- "x N" or "xN" or a plain number before the item = quantity for that item
- "(N)" immediately after a name = that specific person's quantity
- "/" between items = separate items, same holder(s)
- If no quantity given, use 1
- Split multi-item, multi-person entries into individual objects
- Total quantities like "x10" on an item-first line are context only; assign per-person qty from "(N)" annotations, else 1
- For comma-separated lines, decide per entry whether it's person-first or item-first based on whether the first token is a known item word
- In person-first format with no dash, everything after the name (and optional qty) is items — split them into separate items if they are clearly distinct equipment words (e.g. "bibs tennis balls" → bibs + tennis balls, NOT "bibs tennis balls" as one item)
- Fix obvious typos in item names (e.g. "tenni s balls" → "tennis balls", "bib s" → "bibs")
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
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": _HOLDINGS_PROMPT + text}],
        temperature=0,
    )
    raw   = response.choices[0].message.content.strip()
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if not match:
        logger.warning("Groq returned no JSON array: %s", raw[:200])
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError as e:
        logger.warning("Groq JSON decode error: %s | raw: %s", e, raw[:200])
        return None


def _apply_holdings(entries: list) -> dict[str, list[str]]:
    """Write entries to DB. Returns {DisplayName: [formatted items]} for reply."""
    by_holder: dict[str, list[str]] = {}
    for e in entries:
        name = resolve_name(str(e["name"]))
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

    # IC awaiting required-items input after picking a training session
    if db.is_ic_or_master(user.id) and context.user_data.get("req_training"):
        training = context.user_data.pop("req_training")
        items = parse_items_list(text)
        if not items:
            await update.message.reply_text("❌ Couldn't parse any items. Try again with `/required`.", parse_mode="Markdown")
            return
        db.set_required_items(training["id"], items)
        lines = [f"✅ *Required for {training['date']} ({training['venue']}):*\n"]
        for item, qty in items:
            lines.append(f"• {fmt(item, qty)}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

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
# GOOGLE SHEETS — ATTENDANCE
# ──────────────────────────────────────────────────────────────

@ic_only
async def cmd_sheetattendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pull today's attendance directly from the Google Sheet."""
    if not _sheets_enabled:
        await update.message.reply_text(
            "❌ Google Sheets not configured.\n"
            "Set `SHEET_ID`, `SHEET_NAME`, and `SHEET_CREDS` in your `.env` file.",
            parse_mode="Markdown",
        )
        return

    target = date.today()
    # Allow optional date arg: /sheetattendance DD/MM/YYYY
    if context.args:
        try:
            target = datetime.strptime(context.args[0], "%d/%m/%Y").date()
        except ValueError:
            await update.message.reply_text("❌ Date format: `DD/MM/YYYY`", parse_mode="Markdown")
            return

    await update.message.reply_text("⏳ Fetching sheet…")
    try:
        result = _sheets.get_attendance(SHEET_ID, SHEET_NAME, SHEET_CREDS, target)
    except Exception as e:
        logger.error("Sheet fetch error: %s", e)
        await update.message.reply_text(f"❌ Couldn't read sheet: {e}")
        return

    if result is None:
        await update.message.reply_text(
            f"❌ No column found for {target.strftime('%-d %b %Y')} in the sheet.\n"
            "Check that the date exists in row 3."
        )
        return

    attendance = result["attendance"]
    if not attendance:
        await update.message.reply_text("❌ No names found in the sheet.")
        return

    # Group by status
    present, late, absent, tbc, no_resp, other = [], [], [], [], [], []
    for name, parsed in attendance.items():
        s = parsed.get("status")
        rname = resolve_name(name)
        if s == "present":
            present.append(rname)
        elif s == "late":
            late.append((rname, parsed))
        elif s == "absent":
            absent.append((rname, parsed))
        elif s == "tbc":
            tbc.append((rname, parsed))
        elif s == "no response":
            no_resp.append(rname)
        else:
            other.append((rname, parsed))

    venue_str = f" · {result['venue']}" if result["venue"] else ""
    time_str  = f" · {result['time']}"  if result["time"]  else ""
    lines = [
        f"📊 *Sheet attendance — {target.strftime('%-d %b %Y')}{venue_str}{time_str}*\n"
    ]

    if present:
        lines.append(f"✅ *Coming ({len(present)}):* {', '.join(present)}")
    if late:
        lines.append(f"\n⏰ *Late ({len(late)}):*")
        for name, p in late:
            detail_parts = []
            if p.get("reason"):
                detail_parts.append(p["reason"])
            if p.get("eta"):
                detail_parts.append(f"ETA {p['eta']}")
            detail = f" ({', '.join(detail_parts)})" if detail_parts else ""
            lines.append(f"  • {name}{detail}")
    if absent:
        lines.append(f"\n❌ *Absent ({len(absent)}):*")
        for name, p in absent:
            reason = f" — {p['reason']}" if p.get("reason") else ""
            lines.append(f"  • {name}{reason}")
    if tbc:
        lines.append(f"\n❓ *TBC ({len(tbc)}):*")
        for name, p in tbc:
            reason = f" ({p['reason']})" if p.get("reason") else ""
            lines.append(f"  • {name}{reason}")
    if no_resp:
        lines.append(f"\n— *No response ({len(no_resp)}):* {', '.join(no_resp)}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def _sheet_poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Background job: poll the Google Sheet every few minutes.
    When attendance changes for today's training, send a diff to the reminder chat.
    """
    global _last_sheet_hash, _last_sheet_data

    if not _sheets_enabled:
        return

    training = db.get_active_training()
    if not training:
        return

    try:
        training_date = datetime.strptime(training["date"], "%d/%m/%Y").date()
    except ValueError:
        return

    if training_date != date.today():
        return

    chat_id = training.get("reminder_chat_id")
    if not chat_id:
        return

    try:
        result = _sheets.get_attendance(SHEET_ID, SHEET_NAME, SHEET_CREDS, training_date)
    except Exception as e:
        logger.warning("Sheet poll failed: %s", e)
        return

    if result is None:
        return

    attendance = result["attendance"]

    # Build a stable hash from sorted name:raw_value pairs
    import hashlib
    col_str  = "|".join(f"{k}:{v}" for k, v in sorted(attendance.items()))
    new_hash = hashlib.md5(col_str.encode()).hexdigest()

    if _last_sheet_hash is None:
        # First poll — just seed state, don't notify
        _last_sheet_hash = new_hash
        _last_sheet_data = dict(attendance)
        return

    if new_hash == _last_sheet_hash:
        return  # Nothing changed

    # Compute diff
    all_names = set(_last_sheet_data) | set(attendance)
    changes = []
    for name in sorted(all_names):
        old = _last_sheet_data.get(name)
        new = attendance.get(name)
        if old != new:
            changes.append((name, old, new))

    _last_sheet_hash = new_hash
    _last_sheet_data = dict(attendance)

    if not changes:
        return

    lines = ["📊 *Sheet update*\n"]
    for name, old, new in changes:
        old_str = _sheets.format_cell_status(old) if old else "—"
        new_str = _sheets.format_cell_status(new) if new else "—"
        lines.append(f"• *{name}*: {old_str} → {new_str}")

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="\n".join(lines),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning("Failed to send sheet update (chat_id=%s): %s", chat_id, e)


async def _auto_attendance_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Runs every morning (default 7 AM SGT).
    If today is a training day according to the Google Sheet, automatically:
      1. Sends the attendance message to the reminder chat
      2. Runs the equipment delegation plan (unless venue is VR)
    """
    if not _sheets_enabled:
        return

    training = db.get_active_training()
    if not training or not training.get("reminder_chat_id"):
        return

    try:
        training_date = datetime.strptime(training["date"], "%d/%m/%Y").date()
    except ValueError:
        return

    if training_date != date.today():
        return

    chat_id = training["reminder_chat_id"]
    venue   = training.get("venue", "")

    try:
        sheet_data = _sheets.get_attendance(SHEET_ID, SHEET_NAME, SHEET_CREDS, training_date)
    except Exception as e:
        logger.warning("Auto-attendance sheet fetch failed: %s", e)
        return

    if sheet_data is None:
        return

    # Build coming list + DB attendees
    coming       = []
    db_attendees = []
    for name, parsed in sheet_data["attendance"].items():
        display = resolve_name(name).title()
        canon   = resolve_name(name)
        s = parsed.get("status")
        if s == "present":
            coming.append(display)
            db_attendees.append((canon, "present", None))
        elif s == "late":
            parts = ["late"]
            if parsed.get("reason"):
                parts.append(parsed["reason"])
            coming.append(f"{display} ({', '.join(parts)})")
            db_attendees.append((canon, "late", parsed.get("eta")))

    if not coming:
        return

    db.set_attendance(training["id"], db_attendees)

    date_str = training_date.strftime("%d/%m/%y")
    time_str = training.get("report_time") or sheet_data.get("time") or "TBC"

    msg_lines = [f"Attendance {date_str}", ""] + coming + ["", f"Location: {venue}", f"Time: {time_str}"]
    try:
        await context.bot.send_message(chat_id=chat_id, text="\n".join(msg_lines))
    except Exception as e:
        logger.warning("Auto-attendance send failed: %s", e)
        return

    # Skip equipment plan for VR
    if venue.upper().startswith("VR"):
        return

    required = db.get_required_items(training["id"])
    if not required:
        return

    attending = {name.lower().strip() for name, parsed in sheet_data["attendance"].items()
                 if parsed.get("status") in ("present", "late")}

    inv_map: dict[str, list[tuple[str, int]]] = {}
    for r in db.get_full_inventory():
        inv_map.setdefault(r["item"], []).append((r["holder"], r["quantity"]))

    bringing: list[tuple[str, str, int]] = []
    passes:   list[tuple[str, str, str, int]] = []
    missing:  list[tuple[str, int]] = []

    for req in required:
        req_item = req["item"]
        req_qty  = req["quantity"]
        holders  = inv_map.get(req_item, [])
        if not holders:
            missing.append((req_item, req_qty))
            continue
        attending_holders = [(h, q) for h, q in holders if h in attending]
        absent_holders    = [(h, q) for h, q in holders if h not in attending]
        covered           = sum(q for _, q in attending_holders)
        for holder, qty in attending_holders:
            bringing.append((holder, req_item, qty))
        remaining = req_qty - covered
        if remaining > 0:
            for holder, qty in absent_holders:
                if remaining <= 0:
                    break
                take     = min(qty, remaining)
                receiver = next(
                    (b[0] for b in bringing if b[1] == req_item),
                    next(iter(sorted(attending)), None),
                )
                if receiver:
                    passes.append((holder, receiver, req_item, take))
                    remaining -= take
            if remaining > 0:
                missing.append((req_item, remaining))

    by_holder: dict[str, list[str]] = {}
    for holder, item, qty in bringing:
        by_holder.setdefault(holder.title(), []).append(fmt(item, qty))

    plan_lines = [f"📋 *Equipment Plan — {date_str} · {venue} · {time_str}*\n"]
    if by_holder:
        plan_lines.append("🟢 *Bringing directly:*")
        for name, items in sorted(by_holder.items()):
            plan_lines.append(f"• {name} → {', '.join(items)}")
        plan_lines.append("")
    if passes:
        plan_lines.append("🔄 *Passes needed:*")
        for from_h, to_h, item, qty in passes:
            plan_lines.append(f"• {from_h.title()} → pass {fmt(item, qty)} to {to_h.title()}")
        plan_lines.append("")
    if missing:
        plan_lines.append("❓ *Not found / shortfall:*")
        for item, qty in missing:
            plan_lines.append(f"• {fmt(item, qty)} — check locker")
        plan_lines.append("")
    if not passes and not missing:
        plan_lines.append("✅ All items covered, no passes needed!")

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="\n".join(plan_lines),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning("Auto-attendance delegation send failed: %s", e)


# ──────────────────────────────────────────────────────────────
# SCHEDULED REMINDERS
# ──────────────────────────────────────────────────────────────

SGT = ZoneInfo("Asia/Singapore")

_REMINDER_1D = (
    "⚠️ *Training tomorrow!*\n\n"
    "Prep checklist:\n"
    "• Check who has what equipment → /inventory\n"
    "• Ask coaches what's needed, then set it → `/required 10 balls, bibs, ...`\n"
    "• Set attendance → /attendance\n"
    "• Run /delegate to see who brings/passes what"
)


async def _reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    try:
        await context.bot.send_message(
            chat_id=job.chat_id,
            text=job.data["message"],
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning("Failed to send reminder (chat_id=%s): %s", job.chat_id, e)


def _schedule_training_reminders(app, training_id: int, date_str: str, chat_id: int) -> int:
    """
    Schedule up to 3 reminder jobs for a training session.
    Returns the number of jobs actually scheduled (skips any that are already past).
    date_str format: DD/MM/YYYY
    """
    try:
        training_date = datetime.strptime(date_str, "%d/%m/%Y").date()
    except ValueError:
        logger.warning("Could not parse training date for reminders: %s", date_str)
        return 0

    now = datetime.now(SGT)
    scheduled = 0

    reminders = [
        # (days before training, hour SGT, minute, message)
        (1, 9, 0, _REMINDER_1D),
    ]

    for days_before, hour, minute, msg in reminders:
        remind_dt = datetime(
            training_date.year, training_date.month, training_date.day,
            hour, minute, 0,
            tzinfo=SGT,
        ) - timedelta(days=days_before)

        if remind_dt <= now:
            continue  # Already past, skip

        job_name = f"training_{training_id}_d{days_before}"
        # Remove any existing job with this name before scheduling
        existing = app.job_queue.get_jobs_by_name(job_name)
        for j in existing:
            j.schedule_removal()

        app.job_queue.run_once(
            _reminder_job,
            when=remind_dt,
            chat_id=chat_id,
            data={"message": msg, "training_id": training_id},
            name=job_name,
        )
        logger.info("Reminder scheduled: %s at %s for chat %s", job_name, remind_dt, chat_id)
        scheduled += 1

    return scheduled


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

async def post_init(app):
    """Register commands so Telegram shows autocomplete when users type /."""
    public_commands = [
        BotCommand("start",           "Welcome message + status"),
        BotCommand("inventory",       "View all holdings or search by item"),
        BotCommand("whohas",          "See what someone is holding"),
        BotCommand("players",         "List all player names in the DB"),
        BotCommand("acceptic",        "Accept a pending IC handover"),
        BotCommand("help",            "Show all available commands"),
        BotCommand("attendance",      "Pick a session and view attendance"),
        BotCommand("attendancepos",   "Attendance grouped by position"),
        BotCommand("sheetattendance", "Pull attendance for a specific date"),
        BotCommand("training",        "Manually create a training session"),
        BotCommand("required",        "Set equipment needed for training"),
        BotCommand("delegate",        "Generate equipment delegation plan"),
        BotCommand("reminderchat",    "Redirect auto-reminders to this chat"),
        BotCommand("setholding",      "Assign an item to someone"),
        BotCommand("removeitem",      "Remove an item from someone"),
        BotCommand("rename",          "Rename a holder"),
        BotCommand("transfer",        "Move an item between holders"),
        BotCommand("update",          "Bulk post-training inventory update"),
        BotCommand("alias",           "Map a sheet name to a display name"),
        BotCommand("unalias",         "Remove a name alias"),
        BotCommand("clear",           "Wipe training, inventory, or all data"),
        BotCommand("handover",        "Hand over IC role to someone"),
        BotCommand("listic",          "List IC and master users"),
        BotCommand("removeic",        "Revoke IC access from a user"),
    ]
    await app.bot.set_my_commands(public_commands)


def main():
    db.init_db(MASTER_ID)
    logger.info("Database initialised. Master ID: %d", MASTER_ID)
    NAME_ALIASES.update(db.get_all_name_aliases())

    purged = db.purge_old_trainings(days=14)
    if purged:
        logger.info("Purged %d training record(s) older than 14 days.", purged)

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Reschedule reminders for any active training that survived a restart
    training = db.get_active_training()
    if training and training["reminder_chat_id"]:
        n = _schedule_training_reminders(
            app, training["id"], training["date"], training["reminder_chat_id"]
        )
        if n:
            logger.info("Rescheduled %d reminder(s) for training #%d on restart.", n, training["id"])

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("ask",         cmd_ask))
    app.add_handler(CommandHandler("inventory",   cmd_inventory))
    app.add_handler(CommandHandler("whohas",      cmd_whohas))
    app.add_handler(CommandHandler("players",     cmd_players))
    app.add_handler(CommandHandler("acceptic",    cmd_acceptic))

    app.add_handler(CommandHandler("setholding",  cmd_setholding))
    app.add_handler(CommandHandler("removeitem",  cmd_removeitem))
    app.add_handler(CommandHandler("rename",      cmd_rename))
    app.add_handler(CommandHandler("transfer",    cmd_transfer))
    app.add_handler(CommandHandler("update",      cmd_update))

    app.add_handler(CommandHandler("training",    cmd_training))
    app.add_handler(CommandHandler("attendance",    cmd_attendance))
    app.add_handler(CommandHandler("attendancepos", cmd_attendancepos))
    app.add_handler(CommandHandler("required",      cmd_required))
    app.add_handler(CommandHandler("delegate",    cmd_delegate))

    app.add_handler(CommandHandler("alias",             cmd_alias))
    app.add_handler(CommandHandler("unalias",           cmd_unalias))

    app.add_handler(CommandHandler("clear",             cmd_clear))
    app.add_handler(CallbackQueryHandler(callback_clear, pattern="^clear_"))
    app.add_handler(CommandHandler("reminderchat",      cmd_reminderchat))
    app.add_handler(CommandHandler("listic",            cmd_listic))
    app.add_handler(CommandHandler("handover",          cmd_handover))
    app.add_handler(CommandHandler("removeic",          cmd_removeic))
    app.add_handler(CommandHandler("sheetattendance",   cmd_sheetattendance))
    app.add_handler(CallbackQueryHandler(callback_attendance_pick, pattern="^att_pick_"))
    app.add_handler(CallbackQueryHandler(callback_attpos_pick,    pattern="^attpos_pick_"))
    app.add_handler(CallbackQueryHandler(callback_required_pick,  pattern="^req_pick_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_holdings))

    # Poll Google Sheet every 5 minutes on training days
    if _sheets_enabled:
        app.job_queue.run_repeating(_sheet_poll_job, interval=300, first=10)
        logger.info("Sheet polling job scheduled (every 5 min).")

        # Auto-attendance: runs daily at 3 PM SGT (day before training)
        now_sgt    = datetime.now(SGT)
        target_3pm = now_sgt.replace(hour=15, minute=0, second=0, microsecond=0)
        if target_3pm <= now_sgt:
            target_3pm += timedelta(days=1)
        seconds_until = (target_3pm - now_sgt).total_seconds()
        app.job_queue.run_repeating(
            _auto_attendance_job,
            interval=86400,       # every 24 hours
            first=seconds_until,
        )
        logger.info("Auto-attendance job scheduled (daily at 15:00 SGT).")

    logger.info("smuHBLogs is running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

from __future__ import annotations
"""
bot.py — smuHBLogs Telegram Bot
Handball team attendance tracker for SMU.

Commands
───────
Public (anyone can DM the bot):
  /start               — welcome + command list
  /attendance                           ← pick session from sheet; view attendance
  /attendancepos                        ← attendance grouped by position (reads sheet71)
  /acceptic            — accept a pending IC handover

IC-only:
  /training [DD/MM/YYYY] [venue] [time] ← optional: manually create training
  /sheetattendance [DD/MM/YYYY]         ← pull attendance for a specific date
  /alias [sheet_name] as [display_name] ← map sheet name to display name
  /unalias [sheet_name]                 ← remove a name alias
  /clear training
  /handover @username
  /reminderchat [@channel | -100id]     ← send reminders and day-before attendance here or to a channel
  /blast                                ← pick session + type, send attendance to the reminder chat now
  /listic

Master-only:
  /removeic @username
"""

import os
import re
import logging
from datetime import datetime, date, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

import database as db
from health import start_health_server

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
SHEET_ID             = os.getenv("SHEET_ID", "")
SHEET_NAME           = os.getenv("SHEET_NAME", "Sheet1")
SHEET_POSITIONS_NAME = os.getenv("SHEET_POSNAME", "sheet71")
SHEET_CREDS          = os.getenv("SHEET_CREDS", "service_account.json")

SGT = ZoneInfo("Asia/Singapore")


def _parse_hhmm(env_key: str, default: str) -> tuple[int, int]:
    """Read an HH:MM (24h) time from the environment, failing fast if invalid."""
    raw = os.getenv(env_key, default)
    try:
        hour, minute = (int(p) for p in raw.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        raise RuntimeError(f"{env_key} must be HH:MM (24h), got {raw!r}")
    return hour, minute


# Daily job times (SGT), both firing the day before a training:
# the attendance post to the reminder chat, and the heads-up DM to ICs.
ATTENDANCE_POST_HOUR, ATTENDANCE_POST_MINUTE = _parse_hhmm("ATTENDANCE_POST_TIME", "15:00")
IC_REMINDER_HOUR, IC_REMINDER_MINUTE         = _parse_hhmm("IC_REMINDER_TIME", "09:00")

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

# ──────────────────────────────────────────────────────────────
# PARSE HELPERS
# ──────────────────────────────────────────────────────────────

# Map nicknames → canonical DB names to prevent double-counting.
# Add entries here whenever a short name causes a duplicate holder.
NAME_ALIASES: dict[str, str] = {
    "ally": "allison",
    "sera": "seraphina",
}


def resolve_name(name: str) -> str:
    """Return the canonical name for a nickname, or the name itself if not aliased."""
    return NAME_ALIASES.get(name.lower().strip(), name.lower().strip())


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
                "• /attendance — pick a session from Google Sheets",
            ]
            keyboard = [["/attendance", "/attendancepos"], ["/training", "/help"]]
        else:
            attendance = db.get_attendance_rows(training["id"])

            lines += [
                f"📅 <b>{training['date']}</b> · {training['venue']} · {training['report_time']}\n",
            ]

            # Attendance status
            if attendance:
                present_count = sum(1 for r in attendance if r["status"] != "absent")
                lines.append(f"✅ Attendance: {present_count} people")
            else:
                lines += [
                    "❌ Attendance not set",
                    "",
                    "<b>Next step:</b> Set attendance",
                    "Run /attendance to pick a session from Google Sheets",
                ]
            keyboard = [["/attendance", "/attendancepos"], ["/sheetattendance", "/help"]]

        lines.append("\n/help — all commands")
    else:
        lines += [
            "Welcome! I'm the smuHBLogs bot, here to help track handball training attendance.\n\nStart here:",
            "✅ /attendance — view attendance for an upcoming session",
            "🧩 /attendancepos — attendance grouped by position",
            "✅ /acceptic — accept a pending IC handover",
        ]
        keyboard = [["/attendance", "/attendancepos"], ["/help"]]

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
        "/acceptic — accept a pending IC handover",
    ]

    if is_ic:
        lines += [
            "",
            "<b>Training:</b>",
            "/training [DD/MM/YYYY] [venue] [time] — manually create a training session",
            "/sheetattendance [DD/MM/YYYY] — pull attendance for a specific date",
            "/reminderchat [@channel?] — send reminders + day-before attendance here (or to a channel)",
        "/blast — pick a session + message type, send it to the reminder chat now",
            "",
            "<b>Admin:</b>",
            "/alias [sheet_name] as [display_name] — map a sheet name to a display name",
            "/unalias [sheet_name] — remove a name alias",
            "/clear training — cancel the current training",
            "/handover @username — hand over IC role",
            "/listic — list who has IC/master access",
        ]
        if role == "master":
            lines.append("/removeic @username — revoke IC access")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


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

    await update.message.reply_text(
        f"📅 *Training created (#{tid})*\n"
        f"• Date: {date_str}\n"
        f"• Venue: {venue.upper()}\n"
        f"• Time: {time_str}\n\n"
        f"*Next step:* reply to the attendance message with `/attendance`, "
        f"or run `/attendance` to pick a session from the sheet.\n\n"
        f"_The attendance list is auto-posted to the reminder chat at "
        f"{ATTENDANCE_POST_HOUR:02d}:{ATTENDANCE_POST_MINUTE:02d} the day before "
        f"(set the destination with /reminderchat)._",
        parse_mode="Markdown",
    )


def _build_attendance_msgs(sheet_data: dict, training) -> tuple[str, Optional[str]]:
    """
    Build the plain-text attendance message.

    Returns (attendance_msg, None) — kept as a tuple for call-site stability.
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

    return att_msg, None


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


async def callback_attendance_pick(update: Update, _context: ContextTypes.DEFAULT_TYPE):
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
            db.create_training(date_for_db, venue, time_str, reminder_chat_id=chat_id)
            row = db.get_training_by_date(date_for_db)
            matched_training = dict(row) if row else None

        att_msg, _ = _build_attendance_msgs(sheet_data, matched_training)

        if not any(
            p.get("status") in ("present", "late")
            for p in sheet_data["attendance"].values()
        ):
            await query.edit_message_text("❌ Nobody is marked as coming in the sheet yet.")
            return

        await query.edit_message_text(att_msg)
    except Exception as e:
        logger.error("Error in callback_attendance_pick: %s", e, exc_info=True)
        await query.edit_message_text(f"❌ Something went wrong: {e}")


# Roster group headers folded into the single "CBs" section of the
# position-grouped message. Order within CBs follows roster column order
# (left-backs, then centers, then right-backs).
_CB_GROUP = "CBs"
_CB_SOURCE_LABELS = {"left-back", "left back", "centers", "center", "right-back", "right back", "backs", "cbs"}


def _display_group(label: str) -> str:
    """Map a roster column header to the label shown in the message."""
    return _CB_GROUP if label.lower().strip() in _CB_SOURCE_LABELS else label


def _build_attendancepos_msg(sheet_data: dict, positions: dict) -> str:
    """
    Build a position-grouped attendance message from sheet data and the
    positions roster. Groups and their order come straight from the roster
    (its column headers), so sheet edits need no code changes — except the
    back columns (left-back/centers/right-back), which are merged into one
    "CBs" section, members listed in roster column order (L, then C, then R).
    """
    training_date = sheet_data["date"]
    venue    = sheet_data.get("venue") or "TBC"
    time_str = sheet_data.get("time")  or "TBC"
    date_str = training_date.strftime("%d/%m/%y")

    attendance = sheet_data["attendance"]

    # Case/whitespace-insensitive name → group lookup (roster headers folded
    # into their display groups), plus each name's roster position for
    # ordering within a group.
    pos_by_name  = {k.lower().strip(): _display_group(v) for k, v in positions.items()}
    roster_index = {k.lower().strip(): i for i, k in enumerate(positions.keys())}
    group_order  = list(dict.fromkeys(_display_group(v) for v in positions.values()))

    groups: dict[str, list[tuple[int, str]]] = {label: [] for label in group_order}
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
        key = name.lower().strip()
        pos = pos_by_name.get(key, "") or pos_by_name.get(resolve_name(name).lower().strip(), "")
        if pos in groups:
            order = roster_index.get(key, roster_index.get(resolve_name(name).lower().strip(), len(roster_index)))
            groups[pos].append((order, display))
        else:
            unknown.append(display)

    lines = [f"Attendance {date_str}", ""]
    for label in group_order:
        members = [display for _, display in sorted(groups[label])]
        if not members:
            continue
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
    mode = context.args[0].lower() if context.args else "training"
    if mode != "training":
        await update.message.reply_text(
            "Usage: `/clear training` — cancel the current scheduled training",
            parse_mode="Markdown",
        )
        return

    keyboard = [[
        InlineKeyboardButton("✅ Confirm", callback_data="clear_confirm_training"),
        InlineKeyboardButton("❌ Cancel",  callback_data="clear_cancel"),
    ]]
    await update.message.reply_text(
        "⚠️ Are you sure you want to *cancel the current training*?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def callback_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "clear_cancel":
        await query.edit_message_text("❌ Cancelled.")
        return

    cleared = db.clear_active_training()
    await query.edit_message_text(
        "🗑️ Current training cleared. Auto-posts and IC reminders for that date are cancelled too "
        "(recreate it with /training or /attendance if needed)."
        if cleared else "❌ No active training to clear."
    )


async def cmd_reminderchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Set the destination for training reminders and the day-before attendance post.

    /reminderchat                       — use the chat it's run in (group, or posted in a channel)
    /reminderchat @channelname          — from a PM: target a public channel
    /reminderchat -1001234567890        — from a PM: target any chat by numeric id

    Channel posts have no sender, but only channel admins can post there, so
    that is authorisation enough; everywhere else requires IC/master.
    """
    msg  = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if chat.type != chat.CHANNEL and (not user or not db.is_ic_or_master(user.id)):
        await msg.reply_text("🔒 IC or master access required.")
        return

    chat_id = chat.id
    if context.args:
        ref = context.args[0]
        try:
            target = await context.bot.get_chat(ref if ref.startswith("@") else int(ref))
            chat_id = target.id
        except ValueError:
            await msg.reply_text(
                "❌ Chat reference must be `@channelname` or a numeric id like `-1001234567890`.",
                parse_mode="Markdown",
            )
            return
        except Exception as e:
            logger.error("reminderchat: could not resolve %r: %s", ref, e)
            await msg.reply_text(
                f"❌ Couldn't find that chat: {e}\n"
                "Make sure the bot has been added to it as an admin."
            )
            return

    # Save globally — the daily auto-post uses this even when no training
    # record exists yet (it creates one from the sheet the day before).
    db.set_setting("reminder_chat", str(chat_id))

    training_row = db.get_active_training()
    if training_row:
        db.set_training_reminder_chat(training_row["id"], chat_id)
    note = (
        "Day-before attendance posts will be sent here automatically "
        "(use /blast to send one now)."
    )

    if chat_id != chat.id:
        # Prove the bot can post in the target chat before claiming success.
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="🔔 This chat will now receive training reminders and attendance posts.",
            )
        except Exception as e:
            logger.error("reminderchat: cannot post to %s: %s", chat_id, e)
            await msg.reply_text(
                f"⚠️ Saved, but I couldn't post there: {e}\n"
                "Add the bot to the channel as an admin with permission to post, then try again."
            )
            return
        await msg.reply_text(
            f"🔔 *Reminders redirected!* Check the channel for a confirmation message.\n{note}",
            parse_mode="Markdown",
        )
        return

    await msg.reply_text(
        f"🔔 *Reminders redirected to this chat!*\n{note}",
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
# FORWARDED ATTENDANCE HANDLER
# ──────────────────────────────────────────────────────────────

async def handle_text_attendance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    IC/master can forward an attendance message like:
        Attendance 18/03/26
        name1
        name2
        Location: MPSH
        Time: 745PM
    A training session is auto-created with attendance set.
    """
    if not db.is_ic_or_master(update.effective_user.id):
        return
    text = (update.message.text or "").strip()
    parsed = parse_attendance_forward(text)
    if not parsed:
        return
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
    Run daily at ATTENDANCE_POST_TIME SGT and, when training is tomorrow, post
    the attendance message to the configured reminder chat exactly once.
    """
    if not _sheets_enabled:
        return

    # Don't fire before the scheduled time — the startup catch-up run would
    # otherwise post early after every deploy/restart.
    now_sgt = datetime.now(SGT)
    if (now_sgt.hour, now_sgt.minute) < (ATTENDANCE_POST_HOUR, ATTENDANCE_POST_MINUTE):
        return

    tomorrow = datetime.now(SGT).date() + timedelta(days=1)
    date_str = tomorrow.strftime("%d/%m/%Y")

    training_row = db.get_training_by_date(date_str)
    training = dict(training_row) if training_row else None

    if training and training.get("attendance_pos_sent_at"):
        return  # already posted for tomorrow

    if not training:
        if db.has_cleared_training(date_str):
            return  # training was cancelled via /clear — stay silent
        # No DB record yet — look for tomorrow's session in the sheet and
        # create one, so the post goes out without any manual setup.
        default_chat = _default_reminder_chat()
        if not default_chat:
            return  # nowhere to post; /reminderchat has never been run
        try:
            sheet_data = _sheets.get_attendance(SHEET_ID, SHEET_NAME, SHEET_CREDS, tomorrow)
        except Exception as e:
            logger.warning("Auto-attendance sheet check failed: %s", e)
            return
        if sheet_data is None:
            return  # no session scheduled tomorrow
        db.create_training(
            date_str,
            sheet_data.get("venue") or "TBC",
            sheet_data.get("time") or "TBC",
            reminder_chat_id=default_chat,
        )
        training = dict(db.get_training_by_date(date_str))
        logger.info("Auto-created training for %s from the sheet.", date_str)

    ok, detail = await _send_attendance_post(context.bot, training)
    if not ok:
        logger.info("Auto-attendance skipped: %s", detail)
        return

    db.mark_attendance_pos_sent(training["id"])
    logger.info("Posted day-before attendance for training #%s", training["id"])


def _default_reminder_chat() -> Optional[int]:
    """The globally configured reminder chat id (set by /reminderchat), if any."""
    val = db.get_setting("reminder_chat")
    try:
        return int(val) if val else None
    except ValueError:
        logger.error("Invalid reminder_chat setting: %r", val)
        return None


async def _send_attendance_post(bot_obj, training: dict) -> tuple[bool, str]:
    """
    Fetch sheet attendance for the training's date and post the normal
    attendance message to its reminder chat (falling back to the global
    default). Returns (ok, failure_reason). Used by the daily job.
    """
    chat_id = training.get("reminder_chat_id") or _default_reminder_chat()
    if not chat_id:
        return False, "no reminder chat set — run /reminderchat first"

    try:
        training_date = datetime.strptime(training["date"], "%d/%m/%Y").date()
    except ValueError:
        return False, f"couldn't parse training date {training['date']!r}"

    try:
        sheet_data = _sheets.get_attendance(SHEET_ID, SHEET_NAME, SHEET_CREDS, training_date)
    except Exception as e:
        logger.error("Attendance post: sheet fetch failed: %s", e)
        return False, f"couldn't read sheet: {e}"
    if sheet_data is None:
        return False, f"no column for {training['date']} in the sheet"

    if not any(
        parsed.get("status") in ("present", "late")
        for parsed in sheet_data["attendance"].values()
    ):
        return False, f"nobody is marked as coming on {training['date']} yet"

    out_msg, _ = _build_attendance_msgs(sheet_data, training)
    try:
        await bot_obj.send_message(chat_id=chat_id, text=out_msg)
    except Exception as e:
        logger.error("Attendance post to chat %s failed: %s", chat_id, e)
        return False, f"couldn't post to the reminder chat: {e}"
    return True, ""


async def _ic_reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Run daily at IC_REMINDER_TIME SGT. When training is tomorrow, DM each IC
    (falling back to the master if there are none) a heads-up, once per day.
    """
    if not _sheets_enabled:
        return

    now_sgt = datetime.now(SGT)
    if (now_sgt.hour, now_sgt.minute) < (IC_REMINDER_HOUR, IC_REMINDER_MINUTE):
        return

    tomorrow = now_sgt.date() + timedelta(days=1)
    date_str = tomorrow.strftime("%d/%m/%Y")

    if db.get_setting("ic_reminder_sent_for") == date_str:
        return

    training_row = db.get_training_by_date(date_str)
    if not training_row and db.has_cleared_training(date_str):
        return  # training was cancelled via /clear — stay silent

    # The sheet is the source of truth: even with a DB record, no column for
    # tomorrow means the session was removed/cancelled — and the auto-post
    # this reminder announces couldn't run without the sheet either.
    try:
        sheet_data = _sheets.get_attendance(SHEET_ID, SHEET_NAME, SHEET_CREDS, tomorrow)
    except Exception as e:
        logger.warning("IC reminder sheet check failed: %s", e)
        return
    if sheet_data is None:
        return  # no session in the sheet tomorrow

    if training_row:
        venue, time_str = training_row["venue"], training_row["report_time"]
    else:
        venue, time_str = sheet_data.get("venue"), sheet_data.get("time")

    auth_rows = db.list_auth()
    targets = [r["user_id"] for r in auth_rows if r["role"] == "ic"]
    if not targets:
        targets = [r["user_id"] for r in auth_rows if r["role"] == "master"]
    if not targets:
        return

    msg = (
        f"⚠️ *Training tomorrow!*\n"
        f"📅 {date_str} · {venue or 'TBC'} · {time_str or 'TBC'}\n\n"
        f"The attendance list will be auto-posted to the channel at "
        f"{ATTENDANCE_POST_HOUR:02d}:{ATTENDANCE_POST_MINUTE:02d}.\n"
        f"• /blast — post it now (normal or by position)\n"
        f"• /attendancepos — preview grouped by position"
    )
    sent = 0
    for uid in targets:
        try:
            await context.bot.send_message(chat_id=uid, text=msg, parse_mode="Markdown")
            sent += 1
        except Exception as e:
            # Telegram only lets bots DM users who have messaged them first.
            logger.warning("IC reminder DM to %s failed: %s", uid, e)

    if sent:
        db.set_setting("ic_reminder_sent_for", date_str)
        logger.info("IC training reminder sent to %d user(s) for %s.", sent, date_str)


@ic_only
async def cmd_blast(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """
    Manually send an attendance message to the reminder chat/channel.
    Step 1: pick which upcoming session. Step 2: pick the message type.
    """
    if not _sheets_enabled:
        await update.message.reply_text("❌ Google Sheets integration is not enabled.")
        return
    if not _default_reminder_chat() and not db.get_active_training():
        await update.message.reply_text(
            "❌ No reminder chat set. Run `/reminderchat` in the channel "
            "(or `/reminderchat @channel` here) first.",
            parse_mode="Markdown",
        )
        return

    try:
        sessions = _sheets.get_upcoming_sessions(SHEET_ID, SHEET_NAME, SHEET_CREDS, limit=3)
    except Exception as e:
        logger.error("Sheet session fetch error in /blast: %s", e)
        await update.message.reply_text(f"❌ Couldn't read sheet: {e}")
        return
    if not sessions:
        await update.message.reply_text("❌ No upcoming training sessions found in the sheet.")
        return

    keyboard = []
    for s in sessions:
        label         = s["date"].strftime("%-d %b") + f"  ·  {s['venue']}  ·  {s['time']}"
        callback_data = f"blast_pick_{s['date'].strftime('%d%m%Y')}"
        keyboard.append([InlineKeyboardButton(label, callback_data=callback_data)])

    await update.message.reply_text(
        "Which training do you want to blast?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def callback_blast_pick(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    """Step 2 of /blast: choose the message type for the picked date."""
    query = update.callback_query
    if not db.is_ic_or_master(query.from_user.id):
        await query.answer("🔒 IC or master access required.", show_alert=True)
        return
    await query.answer()

    date_str = query.data.replace("blast_pick_", "")  # DDMMYYYY
    keyboard = [[
        InlineKeyboardButton("📋 Normal",      callback_data=f"blast_type_{date_str}_norm"),
        InlineKeyboardButton("🧩 By position", callback_data=f"blast_type_{date_str}_pos"),
    ]]
    await query.edit_message_text(
        "What type of attendance message?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def callback_blast_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Final step of /blast: build the chosen message and send it to the reminder chat."""
    query = update.callback_query
    if not db.is_ic_or_master(query.from_user.id):
        await query.answer("🔒 IC or master access required.", show_alert=True)
        return
    await query.answer()

    m = re.match(r"^blast_type_(\d{8})_(norm|pos)$", query.data)
    if not m:
        await query.edit_message_text("❌ Invalid selection.")
        return
    try:
        target_date = datetime.strptime(m.group(1), "%d%m%Y").date()
    except ValueError:
        await query.edit_message_text("❌ Invalid date.")
        return
    msg_type = m.group(2)

    date_for_db  = target_date.strftime("%d/%m/%Y")
    training_row = db.get_training_by_date(date_for_db)
    training     = dict(training_row) if training_row else None
    chat_id      = (training or {}).get("reminder_chat_id") or _default_reminder_chat()
    if not chat_id:
        await query.edit_message_text(
            "❌ No reminder chat set. Run /reminderchat in the channel "
            "(or /reminderchat @channel here) first."
        )
        return

    await query.edit_message_text(f"⏳ Fetching sheet for {target_date.strftime('%-d %b %Y')}…")
    try:
        sheet_data = _sheets.get_attendance(SHEET_ID, SHEET_NAME, SHEET_CREDS, target_date)
    except Exception as e:
        logger.error("Sheet fetch error in blast_type: %s", e)
        await query.edit_message_text(f"❌ Couldn't read sheet: {e}")
        return
    if sheet_data is None:
        await query.edit_message_text(
            f"❌ No column for {target_date.strftime('%-d %b %Y')} found in the sheet."
        )
        return
    if not any(
        p.get("status") in ("present", "late")
        for p in sheet_data["attendance"].values()
    ):
        await query.edit_message_text("❌ Nobody is marked as coming in the sheet yet.")
        return

    try:
        if msg_type == "pos":
            try:
                positions = _sheets.get_positions(SHEET_ID, SHEET_POSITIONS_NAME, SHEET_CREDS)
            except Exception as e:
                logger.warning("Blast: positions fetch failed: %s", e)
                positions = {}
            out_msg = _build_attendancepos_msg(sheet_data, positions)
        else:
            out_msg, _ = _build_attendance_msgs(sheet_data, training)

        await context.bot.send_message(chat_id=chat_id, text=out_msg)
    except Exception as e:
        logger.error("Blast post to chat %s failed: %s", chat_id, e, exc_info=True)
        await query.edit_message_text(
            f"❌ Couldn't post to the reminder chat: {e}\n"
            "Make sure the bot is an admin there."
        )
        return

    type_label = "position-grouped" if msg_type == "pos" else "normal"
    await query.edit_message_text(
        f"✅ Sent the {type_label} attendance for {date_for_db} to the reminder chat."
    )


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

async def post_init(app):
    """Register commands so Telegram shows autocomplete when users type /."""
    public_commands = [
        BotCommand("start",           "Welcome message + status"),
        BotCommand("help",            "Show all available commands"),
        BotCommand("attendance",      "Pick a session and view attendance"),
        BotCommand("attendancepos",   "Attendance grouped by position"),
        BotCommand("sheetattendance", "Pull attendance for a specific date"),
        BotCommand("training",        "Manually create a training session"),
        BotCommand("reminderchat",    "Redirect auto-reminders to this chat"),
        BotCommand("blast",           "Send the attendance post to the channel now"),
        BotCommand("acceptic",        "Accept a pending IC handover"),
        BotCommand("alias",           "Map a sheet name to a display name"),
        BotCommand("unalias",         "Remove a name alias"),
        BotCommand("clear",           "Cancel the current training"),
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

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("acceptic",    cmd_acceptic))

    app.add_handler(CommandHandler("training",      cmd_training))
    app.add_handler(CommandHandler("attendance",    cmd_attendance))
    app.add_handler(CommandHandler("attendancepos", cmd_attendancepos))

    app.add_handler(CommandHandler("alias",             cmd_alias))
    app.add_handler(CommandHandler("unalias",           cmd_unalias))

    app.add_handler(CommandHandler("clear",             cmd_clear))
    app.add_handler(CallbackQueryHandler(callback_clear, pattern="^clear_"))
    app.add_handler(CommandHandler(
        "reminderchat", cmd_reminderchat,
        filters=filters.UpdateType.MESSAGES | filters.UpdateType.CHANNEL_POST,
    ))
    app.add_handler(CommandHandler("blast",             cmd_blast))
    app.add_handler(CallbackQueryHandler(callback_blast_pick, pattern="^blast_pick_"))
    app.add_handler(CallbackQueryHandler(callback_blast_type, pattern="^blast_type_"))
    app.add_handler(CommandHandler("listic",            cmd_listic))
    app.add_handler(CommandHandler("handover",          cmd_handover))
    app.add_handler(CommandHandler("removeic",          cmd_removeic))
    app.add_handler(CommandHandler("sheetattendance",   cmd_sheetattendance))
    app.add_handler(CallbackQueryHandler(callback_attendance_pick, pattern="^att_pick_"))
    app.add_handler(CallbackQueryHandler(callback_attpos_pick,    pattern="^attpos_pick_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_attendance))

    # Poll Google Sheet every 5 minutes on training days
    if _sheets_enabled:
        app.job_queue.run_repeating(_sheet_poll_job, interval=300, first=10)
        logger.info("Sheet polling job scheduled (every 5 min).")

        # Daily day-before jobs: attendance post to the reminder chat, and the
        # heads-up DM to ICs. The run_once calls catch up after restarts; each
        # job's time gate + sent marker make them once-only.
        def _seconds_until(hour: int, minute: int) -> float:
            now_sgt = datetime.now(SGT)
            target = now_sgt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now_sgt:
                target += timedelta(days=1)
            return (target - now_sgt).total_seconds()

        app.job_queue.run_repeating(
            _auto_attendance_job, interval=86400,
            first=_seconds_until(ATTENDANCE_POST_HOUR, ATTENDANCE_POST_MINUTE),
        )
        app.job_queue.run_once(_auto_attendance_job, when=10)
        app.job_queue.run_repeating(
            _ic_reminder_job, interval=86400,
            first=_seconds_until(IC_REMINDER_HOUR, IC_REMINDER_MINUTE),
        )
        app.job_queue.run_once(_ic_reminder_job, when=15)
        logger.info(
            "Daily jobs scheduled: attendance post %02d:%02d SGT, IC reminder %02d:%02d SGT.",
            ATTENDANCE_POST_HOUR, ATTENDANCE_POST_MINUTE, IC_REMINDER_HOUR, IC_REMINDER_MINUTE,
        )

    # Open the health endpoint when PORT is set (Render web service + keep-alive pings)
    start_health_server()

    logger.info("smuHBLogs is running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

# smuHBLogs Bot — Dev Guidelines for Claude

## After every code change, always update these two things

### 1. `/help` command (`cmd_help` in bot.py)
- Add any new command to the correct section: **Anyone**, **Training**, or **Admin**
- Remove or rename commands that are changed or deleted
- Keep descriptions short (one line, action-oriented: what it does, not how it works)
- IC-only commands go inside the `if is_ic:` block; master-only go inside `if role == "master":`

### 2. The docstring at the top of `bot.py`
- The `Commands` block at the top of the file is the developer-facing reference
- Mirror any additions/removals from `/help` here too

---

## Command design rules

**Check existing functions:**
- If modified one function, ensure all related functions or similar functions are modified to use the new function properly, without error. Example: `/attendance` and `/attendancepos` share the sheet-picker flow.
- always ensure all functions are working with every change

**Be consistent with existing patterns:**
- `/attendance`, `/attendancepos`, and `/blast` show a date-picker keyboard with 7 upcoming sessions
- Callback data prefixes must be unique: `att_pick_`, `attpos_pick_`, `clear_`, `blast_pick_`, `blast_type_`, etc.
- IC-only commands use the `@ic_only` decorator
- Master-only commands check `role == "master"` inside the handler

**Message format conventions:**
- Attendance messages: plain text, no markdown (sent via `edit_message_text` without `parse_mode`)
- Status/confirmation messages: Markdown, prefixed with emoji (✅ ❌ ⏳ ℹ️)
- Help and plan messages: HTML (`parse_mode="HTML"`)
- Always include `Location:` and `Time:` at the bottom of attendance-style messages

**Error handling:**
- Sheet fetch failures → show `❌ Couldn't read sheet: {e}`, log with `logger.error`
- No data found → friendly message explaining what's missing and what to do next
- Permission failures → `🔒 IC or master access required.`


---

## Environment variables

| Variable           | Default          | Purpose                              |
|--------------------|------------------|--------------------------------------|
| `BOT_TOKEN`        | —                | Telegram bot token (required)        |
| `MASTER_ID`        | `605114234`      | Telegram user ID of the master       |
| `SHEET_ID`         | team workbook ID | Google Sheets spreadsheet ID         |
| `SHEET_NAME`       | `Jan - Dec 2026` | Tab name for attendance tracking     |
| `SHEET_POSNAME`    | `Positions`      | Tab name for the positions roster    |
| `SHEET_CREDS`      | `service_account.json` | Path or raw JSON for GCP creds |
| `DATABASE_URL`     | —                | Postgres URI (Supabase); when set, used instead of SQLite |
| `DB_PATH`          | `hblogs.db`      | SQLite file path (local dev only)    |
| `PORT`             | —                | If set, serves an HTTP health endpoint (Render/uptime pings) |
| `ATTENDANCE_POST_TIME` | `15:00`      | Time (SGT, 24h HH:MM) of the daily day-before attendance post |
| `IC_REMINDER_TIME` | `09:00`          | Time (SGT, 24h HH:MM) of the day-before heads-up DM to ICs |

Storage backend: `database.py` uses Postgres when `DATABASE_URL` is set, SQLite otherwise.
Deployment: see `deploy/DEPLOY.md` (Render free tier + Supabase + UptimeRobot).

---

## Sheet layouts (read-only)

### Attendance sheet (`SHEET_NAME`)
| Row (0-based) | Content |
|---|---|
| 0 | Instructions / header text |
| 1 | Venue per session column |
| 2 | Date headers e.g. `17 Mar, Tues` |
| 3 | Warmup/report times |
| 4+ | Player name (col A), attendance per date column |

Cell values: `1` = present, `0` = absent, `1 (late, work, 8pm)` = late, `tbc` = TBC, blank = no response.

### Positions roster (`SHEET_POSNAME` / "Positions" tab)
Columnar layout — one column of player names per position group. Title/note
rows may sit above the header row; the parser scans the first 10 rows for the
first row with ≥2 header-like cells (non-blank, non-numeric, not `No.`):
| Row | Content |
|---|---|
| top | Optional title/note rows (merged cells — ignored) |
| header | Group headers, e.g. `LEFT-BACK`, `No.`, `CENTERS`, `No.`, `RIGHT-BACK`, `WINGERS`, `PIVOTS`, `KEEPERS` |
| below | Player names under each group header (`No.` columns / numeric cells skipped) |

Groups and their display order come straight from the headers (title-cased) —
new/renamed groups in the sheet need no code changes — **except** the back
columns (`LEFT-BACK`/`CENTERS`/`RIGHT-BACK`, see `_CB_SOURCE_LABELS` in
bot.py), which are merged into one `CBs` section in `/attendancepos` output,
members listed in roster column order (all L, then C, then R). Members of
every group are listed in roster order, not attendance-sheet order. A legacy
fallback still parses the old row layout (col A = name, col B = position,
data from row 5). Rows named `Total` are ignored in both the roster and the
attendance tab.

---

## Current commands at a glance

### Anyone
- `/start` — welcome message + status
- `/attendance` — pick from the next 7 sessions (view attendance)
- `/attendancepos` — same 7-session picker, grouped by position
- `/acceptic` — accept a pending IC handover
- `/help` — this list

### IC-only (Training)
- `/training [DD/MM/YYYY] [venue] [time]` — manually create a training session (optional)
- `/sheetattendance [DD/MM/YYYY]` — pull attendance for a specific date
- `/reminderchat [@channel | -100id]` — redirect auto-reminders + day-before attendance post to the current chat, or to a channel by reference (from a PM); also works posted directly in a channel the bot administers
- `/blast` — two-step picker (next 7 sessions → normal/position-grouped), then sends that attendance message to the reminder chat immediately; ignores the once-only marker and scheduled time

**Auto-post behaviour:** daily at `ATTENDANCE_POST_TIME` SGT, the bot checks the sheet for a session dated tomorrow. If found, it auto-creates the training record (no manual `/training` needed) and posts the **normal attendance message** to the default reminder chat (`settings.reminder_chat`, set by `/reminderchat`). The `attendance_pos_sent_at` marker ensures it posts once per training. Position-grouped posts are available manually via `/blast` or `/attendancepos`.

**IC heads-up:** daily at `IC_REMINDER_TIME` SGT (default 09:00), when training is tomorrow, the bot DMs every `ic`-role user (master if none) a reminder that the auto-post is coming, once per day (`settings.ic_reminder_sent_for` marker). There is no channel prep reminder — that was removed in favour of these DMs.

**Cancellation semantics (both daily jobs):** the sheet is the source of truth — if there's no column for tomorrow, nothing is sent even when a DB training record exists. A training cleared via `/clear` leaves a `status='cleared'` row that also suppresses auto-creation/posting for that date, even if the sheet still has the column.
- Forwarding an `Attendance DD/MM/YY` message to the bot auto-creates a training + attendance

### IC-only (Admin)
- `/alias [sheet_name] as [display_name]` — map a sheet name to a display name; `/alias` alone lists all
- `/unalias [sheet_name]` — remove a name alias
- `/clear training` — cancel the current training
- `/handover @username` — hand over IC role
- `/listic` — list IC/master users

### Master-only
- `/removeic @username` — revoke IC access

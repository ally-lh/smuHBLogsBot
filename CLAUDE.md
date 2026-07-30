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
- Commands that read from Google Sheets show a date-picker keyboard (3 upcoming sessions as buttons)
- Callback data prefixes must be unique: `att_pick_`, `attpos_pick_`, `clear_`, etc.
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
| `SHEET_ID`         | —                | Google Sheets spreadsheet ID         |
| `SHEET_NAME`       | `Sheet1`         | Tab name for attendance tracking     |
| `SHEET_POSNAME`    | `sheet71`        | Tab name for the positions roster    |
| `SHEET_CREDS`      | `service_account.json` | Path or raw JSON for GCP creds |
| `GROQ_API_KEY`     | —                | Optional: Groq AI key                |
| `DATABASE_URL`     | —                | Postgres URI (Supabase); when set, used instead of SQLite |
| `DB_PATH`          | `hblogs.db`      | SQLite file path (local dev only)    |
| `PORT`             | —                | If set, serves an HTTP health endpoint (Render/uptime pings) |

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

### Positions roster (`SHEET_POSNAME` / sheet71)
| Row (0-based) | Content |
|---|---|
| 0–2 | Header / instructions (skipped) |
| 3 | "Start Warmup at" label (skipped) |
| 4+ | Col A = player name, Col B = position |

Valid positions: `Goalkeeper`, `Pivot`, `Back`, `Wing`
Display labels:  `Keeper`,     `Pivots`, `CBs`, `Wings`

---

## Current commands at a glance

### Anyone
- `/start` — welcome message + status
- `/attendance` — pick from upcoming sessions (view attendance)
- `/attendancepos` — same as /attendance but grouped by position
- `/acceptic` — accept a pending IC handover
- `/ask [question]` — ask a question about commands or attendance
- `/help` — this list

### IC-only (Training)
- `/training [DD/MM/YYYY] [venue] [time]` — manually create a training session (optional)
- `/sheetattendance [DD/MM/YYYY]` — pull attendance for a specific date
- `/reminderchat` — redirect auto-reminders to current chat
- Forwarding an `Attendance DD/MM/YY` message to the bot auto-creates a training + attendance

### IC-only (Admin)
- `/alias [sheet_name] as [display_name]` — map a sheet name to a display name; `/alias` alone lists all
- `/unalias [sheet_name]` — remove a name alias
- `/clear training` — cancel the current training
- `/handover @username` — hand over IC role
- `/listic` — list IC/master users

### Master-only
- `/removeic @username` — revoke IC access

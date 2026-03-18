# smuHBLogs Bot — Dev Guidelines for Claude

## After every code change, always update these two things

### 1. `/help` command (`cmd_help` in bot.py, line ~328)
- Add any new command to the correct section: **Anyone**, **Training**, **Inventory**, or **Admin**
- Remove or rename commands that are changed or deleted
- Keep descriptions short (one line, action-oriented: what it does, not how it works)
- IC-only commands go inside the `if is_ic:` block; master-only go inside `if role == "master":`

### 2. The docstring at the top of `bot.py` (lines 1–31)
- The `Commands` block at the top of the file is the developer-facing reference
- Mirror any additions/removals from `/help` here too

---

## Command design rules

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
- `/inventory [item?]` — view all holdings or search by item
- `/whohas [name]` — see what someone holds
- `/players` — list all player names currently in the DB
- `/acceptic` — accept a pending IC handover
- `/help` — this list

### IC-only (Training)
- `/attendance` — pick a session from sheet, auto-creates training record if needed, save to DB
- `/training [DD/MM/YYYY] [venue] [time]` — manually create a training session (optional)
- `/attendancepos` — same picker, output grouped by position (reads from sheet71)
- `/sheetattendance [DD/MM/YYYY]` — pull attendance for a specific date
- `/required [items, ...]` — set equipment needed
- `/delegate` — generate equipment delegation plan + copy-paste message
- `/reminderchat` — redirect auto-reminders to current chat

### IC-only (Inventory)
- `/setholding [name] [qty?] [item]` — assign item
- `/removeitem [name] [item]` — remove item
- `/rename [old] to [new]` — rename a holder
- `/transfer [item] from [name] to [name]` — move item
- `/update [name] [qty?] [item], ...` — bulk post-training update

### IC-only (Admin)
- `/clear training|inventory|all` — wipe data
- `/handover @username` — hand over IC role
- `/listic` — list IC/master users

### Master-only
- `/removeic @username` — revoke IC access

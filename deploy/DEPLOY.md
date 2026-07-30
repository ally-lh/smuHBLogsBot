# Deploying smuHBLogs — free forever (Render + Supabase)

Architecture: the bot runs 24/7 as a **Render free web service** (kept awake
by a free uptime pinger), and its data lives in **Supabase free Postgres**
(persistent — Render's free disk is wiped on every restart). No credit card
is needed for any of the three services.

## 1. Supabase (database)

1. Sign up at https://supabase.com (GitHub login works), create a project.
   Region: Singapore. Save the database password you set.
2. In the project: **Connect** (top bar) → **Session pooler** → copy the URI.
   It looks like:
   `postgresql://postgres.abcdefgh:[PASSWORD]@aws-1-ap-southeast-1.pooler.supabase.com:5432/postgres`
   - **Must be the Session pooler URI**, not "Direct connection" — direct is
     IPv6-only and Render can't reach it.
   - Replace `[PASSWORD]` with your actual password.
3. Migrate your existing data (run locally, from the project root):
   ```bash
   .venv/bin/pip install psycopg2-binary
   DATABASE_URL="postgresql://...pooler.supabase.com:5432/postgres" \
     .venv/bin/python deploy/migrate_to_supabase.py
   ```
   This copies IC roles, name aliases, and training/attendance records.
   Safe to re-run.

## 2. Render (bot hosting)

1. Sign up at https://render.com (GitHub login). **New → Web Service**, pick
   this repo (push it to GitHub first if it isn't there).
2. Render reads `render.yaml`; otherwise set: runtime Python,
   build `pip install -r requirements.txt`, start `python bot.py`,
   instance type **Free**.
3. In the service's **Environment** tab set:

   | Key            | Value                                                   |
   |----------------|---------------------------------------------------------|
   | `BOT_TOKEN`    | your Telegram bot token                                 |
   | `DATABASE_URL` | the Supabase Session-pooler URI from step 1             |
   | `SHEET_ID`     | your Google Sheet ID                                    |
   | `SHEET_NAME`   | attendance tab name                                     |
   | `SHEET_POSNAME`| positions tab name                                      |
   | `SHEET_CREDS`  | **paste the full service-account JSON as the value**    |
   | `GROQ_API_KEY` | (optional) for /ask                                     |
   | `MASTER_ID`    | your Telegram user ID                                   |

   `SHEET_CREDS` takes raw JSON on Render — there's no file on disk.
   Don't set `DB_PATH`; with `DATABASE_URL` set the bot uses Postgres.
4. Deploy. Logs should show `Database initialised`, `Health endpoint
   listening`, and `smuHBLogs is running.`
5. **Stop the Railway deployment now** — two bots polling one token conflict.

## 3. Keep-alive pinger (so scheduled jobs never sleep)

Render free services idle after ~15 min without traffic; a sleeping bot can't
send the 3 PM attendance post. Fix with a free pinger:

1. Sign up at https://uptimerobot.com (or cron-job.org).
2. New monitor: HTTP(S), URL = your Render service URL
   (`https://smuhblogs.onrender.com/`), interval **5 minutes**.

The bot's health endpoint answers these pings; the free 750 instance-hours
per month cover a full month of 24/7 uptime for one service.

## Verifying

- `/start` in Telegram → replies with your role badge.
- `/listic` → shows the IC/master list migrated from SQLite.
- Render logs (Dashboard → Logs) show sheet polling every 5 min on training
  days and the auto-attendance job scheduled at 15:00 SGT
  (configurable via `ATTENDANCE_POST_TIME`, 24h HH:MM).

## Notes

- **Supabase pausing:** free projects pause after ~7 days with no activity;
  the bot queries the DB on every command and daily jobs, so normal usage
  keeps it alive. If it ever pauses (long holiday), un-pause it from the
  Supabase dashboard.
- **Local dev is unchanged:** without `DATABASE_URL`, the bot uses the local
  `hblogs.db` SQLite file as before.
- `smuhblogs.service` + `setup.sh` in this folder are for the alternative
  VM-based deploy (Azure/Oracle/DigitalOcean) and aren't used by Render.

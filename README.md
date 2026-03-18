# smuHBLogs

Telegram bot for managing SMU handball team logistics — tracking who holds what equipment, delegating gear for training, and handing over IC duties each month.

---

## Setup (one-time, ~10 min)

### 1. Create the bot on Telegram

1. Open Telegram and DM **@BotFather**
2. Send `/newbot`
3. Name: `smuHBLogs`
4. Username: `smuHBLogsBot` (or similar, must end in `bot`)
5. BotFather gives you a **token** — copy it, you'll need it in step 3

### 2. Push to GitHub

```bash
git init
git add .
git commit -m "initial commit"
# Create a new repo on github.com, then:
git remote add origin https://github.com/YOUR_USERNAME/smuHBLogs.git
git push -u origin main
```

### 3. Deploy to Railway

1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. Select your `smuHBLogs` repo
3. Once imported, go to **Variables** tab and add:

   | Key | Value |
   |-----|-------|
   | `BOT_TOKEN` | (paste from BotFather) |
   | `MASTER_ID` | your Telegram user ID |
   | `SHEET_ID` | your Google Sheets spreadsheet ID |
   | `SHEET_NAME` | tab name for attendance (default: `Sheet1`) |
   | `SHEET_POSNAME` | tab name for positions roster (default: `sheet71`) |
   | `SHEET_CREDS` | path or raw JSON of your GCP service account |

4. Railway auto-detects the `Procfile` and starts the bot
5. Check the **Logs** tab — you should see `smuHBLogs is running.`

> **Note on the database**: Railway's filesystem is ephemeral on the free tier — the DB resets on redeploy. For persistence, add a Railway **Volume** (Storage tab → Add Volume → mount at `/app`) and set `DB_PATH=/app/hblogs.db`. Or just re-run `/update` after a redeploy to re-log inventory (takes 30 sec).

---

## First-time use

After the bot is live, DM it on Telegram:

```
/start
```

You'll see your role is 👑 Master. Now log the current inventory:

```
/update ella 4 balls, ruhan 2 balls, allison 2 balls, nydia 2 balls, denise 2 balls, rena bibs, allison bands, eunice tape bag, nicole marker discs
```

Done. The bot knows who has what.

> **Name aliases**: The bot maps common nicknames to canonical names automatically (e.g. `ally` → `allison`, `sera` → `seraphina`). Add more aliases in `NAME_ALIASES` in `bot.py`.

---

## Typical training workflow

**Day before training — coach sends equipment list:**

```
/training 11/02/2026 jurong 7:30pm
/required 10 balls, bibs, bands, tape bag, marker discs, tennis balls
```

**Attendance message comes in — reply to it:**

```
/attendance
```

**Generate the delegation plan:**

```
/delegate
```

Bot outputs:
- Who brings what directly
- Who needs to pass gear to someone attending
- What's unaccounted for (check MPSH locker)
- A ready-to-copy message for the group

**After training — log who took what home:**

```
/update ella 4 balls, ruhan 2 balls, allison 2 balls + bands, rena bibs, eunice tape bag
```

---

## All commands

### Anyone

| Command | Description |
|---------|-------------|
| `/start` | Welcome message + status |
| `/inventory` | See all equipment holdings |
| `/inventory [item]` | Who has a specific item |
| `/whohas [name]` | What someone is holding |
| `/players` | List all player names in the DB |
| `/acceptic` | Accept a pending IC handover |
| `/help` | Full command list |

### IC-only — Training

| Command | Description |
|---------|-------------|
| `/training [DD/MM/YYYY] [venue] [time]` | Create a training session |
| `/attendance` | Pick a session, pull attendance from sheet, save to DB |
| `/attendancepos` | Same picker, output grouped by position |
| `/sheetattendance [DD/MM/YYYY]` | Pull attendance for a specific date |
| `/required [items, ...]` | Set equipment needed for training |
| `/delegate` | Generate equipment delegation plan |
| `/reminderchat` | Redirect auto-reminders to current chat |

### IC-only — Inventory

| Command | Description |
|---------|-------------|
| `/setholding [name] [qty?] [item]` | Assign item to someone |
| `/removeitem [name] [item]` | Remove item from someone |
| `/rename [old] to [new]` | Rename a holder |
| `/transfer [item] from [name] to [name]` | Move item between holders |
| `/update [name] [qty?] [item], ...` | Bulk post-training update |

### IC-only — Admin

| Command | Description |
|---------|-------------|
| `/clear training\|inventory\|all` | Wipe data |
| `/handover @username` | Hand over IC role |
| `/listic` | List who has IC/master access |

### Master-only

| Command | Description |
|---------|-------------|
| `/removeic @username` | Revoke IC access |

---

## Monthly handover

Current IC:
```
/handover @newperson
```

New person DMs the bot:
```
/acceptic
```

That's it. Old IC is removed, new IC is in. Master access is permanent and unaffected.

---

## Tips

- **Item names must match** between `/setholding` and `/required`. Use consistent names (e.g. always `balls` not `ball` or `handball`). The bot matches by substring so `ball` will find `balls`.
- **Training cancelled?** `/clear training` wipes it cleanly without touching inventory.
- **New month, fresh start?** `/clear all` resets everything except the IC list.
- **Someone passes equipment at school?** Use `/transfer` to update the holder, or `/setholding` to directly set the new holder.
- **Check who's in the DB?** `/players` lists every name currently holding something.

# smuHBLogs 🏐

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
   | `MASTER_ID` | `605114234` |
   | `DB_PATH` | `hblogs.db` |

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
/update ella 4 balls, ruhan 2 balls, ally 2 balls, nydia 2 balls, denise 2 balls, rena bibs, ally bands, eunice tape bag, nicole marker discs
```

Done. The bot knows who has what.

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
(just reply to the attendance message with this command)

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
/update ella 4 balls, ruhan 2 balls, ally 2 balls + bands, rena bibs, eunice tape bag
```

---

## All commands

| Command | Who | Description |
|---------|-----|-------------|
| `/inventory` | Anyone | See all holdings |
| `/inventory [item]` | Anyone | Who has a specific item |
| `/whohas [name]` | Anyone | What someone is holding |
| `/setholding [name] [qty?] [item]` | IC | Set one person's holding |
| `/removeitem [name] [item]` | IC | Remove an item from someone |
| `/transfer [item] from [name] to [name]` | IC | Mark a pass as done |
| `/update [name] [qty?] [item], ...` | IC | Bulk update after training |
| `/training [date] [venue] [time]` | IC | Create upcoming training |
| `/attendance` | IC | Set attendance (reply to msg) |
| `/required [items, ...]` | IC | Set required equipment |
| `/delegate` | IC | Generate delegation plan |
| `/clear training\|inventory\|all` | IC | Reset training / inventory |
| `/handover @username` | IC | Initiate IC handover |
| `/acceptic` | New IC | Confirm handover |
| `/listic` | IC | View who has access |
| `/removeic @username` | Master | Revoke IC access |

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

That's it. Old IC is removed, new IC is in. Your master access is permanent and unaffected.

---

## Tips

- **Item names must match** between `/setholding` and `/required`. Use consistent names (e.g. always `balls` not `ball` or `handball`). The bot matches by substring so `ball` will find `balls`.
- **Training cancelled?** `/clear training` wipes it cleanly without touching inventory.
- **New month, fresh start?** `/clear all` resets everything except the IC list.
- **Someone passes equipment at school?** Use `/transfer` to update the holder, or `/setholding` to directly set the new holder.

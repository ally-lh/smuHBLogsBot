"""
One-time migration: copy the local SQLite data into Supabase Postgres.

Run locally from the project root, with DATABASE_URL set to the Supabase
*Session pooler* connection string:

    DATABASE_URL="postgresql://..." .venv/bin/python deploy/migrate_to_supabase.py

Copies auth, name_aliases, training, and attendance. Existing rows with the
same primary key are left untouched, so re-running is safe.
"""

import os
import sqlite3
import sys

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_PATH = os.getenv("SQLITE_PATH", "hblogs.db")

if not DATABASE_URL:
    sys.exit("❌ DATABASE_URL is not set. Paste the Supabase Session pooler URI.")
if not os.path.exists(DB_PATH):
    sys.exit(f"❌ SQLite file not found: {DB_PATH} (set SQLITE_PATH to override)")

try:
    import psycopg2
except ImportError:
    sys.exit("❌ psycopg2 missing — run: .venv/bin/pip install psycopg2-binary")

import database  # noqa: E402  (imports with DATABASE_URL set → Postgres mode)

if not database._PG:
    sys.exit("❌ database.py did not pick up DATABASE_URL — aborting.")

MASTER_ID = int(os.getenv("MASTER_ID", "605114234"))

print(f"Source:  {DB_PATH}")
print("Target:  Supabase (Postgres)")

print("Creating tables…")
database.init_db(MASTER_ID)

lite = sqlite3.connect(DB_PATH)
lite.row_factory = sqlite3.Row
pg = psycopg2.connect(DATABASE_URL)
cur = pg.cursor()


def copy(table: str, columns: list[str], conflict_key: str) -> None:
    try:
        rows = lite.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
    except sqlite3.OperationalError as e:
        print(f"  {table}: skipped ({e})")
        return
    placeholders = ", ".join(["%s"] * len(columns))
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict_key}) DO NOTHING"
    )
    for row in rows:
        cur.execute(sql, tuple(row))
    print(f"  {table}: {len(rows)} row(s)")


print("Copying data…")
copy("auth", ["user_id", "username", "role"], "user_id")
copy("name_aliases", ["sheet_name", "display_name"], "sheet_name")
copy(
    "training",
    ["id", "date", "venue", "report_time", "status", "created_at",
     "reminder_chat_id", "attendance_pos_sent_at"],
    "id",
)
copy("attendance", ["id", "training_id", "name", "status", "late_time"], "id")

# Explicit ids were inserted for training — move its sequence past the max id.
cur.execute(
    "SELECT setval(pg_get_serial_sequence('training', 'id'), "
    "COALESCE((SELECT MAX(id) FROM training), 1))"
)
cur.execute(
    "SELECT setval(pg_get_serial_sequence('attendance', 'id'), "
    "COALESCE((SELECT MAX(id) FROM attendance), 1))"
)

pg.commit()
pg.close()
lite.close()
print("✅ Migration complete. Verify with: /listic and /alias once the bot is up.")

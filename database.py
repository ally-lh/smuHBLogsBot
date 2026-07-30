"""
database.py — storage layer for smuHBLogs
All DB reads/writes go through here. bot.py never touches SQL directly.

Backend is chosen at startup:
- DATABASE_URL set (deployment) → Postgres (e.g. Supabase) via psycopg2
- otherwise (local dev)         → SQLite file at DB_PATH

Rows are returned as mappings: access columns by name (row["col"]) and
dict(row) works on both backends. All names/items are lowercased before
writing, so plain TEXT comparisons behave case-insensitively.
"""

import os
import sqlite3
from typing import Any, Optional

DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_PATH = os.getenv("DB_PATH", "hblogs.db")
_PG = bool(DATABASE_URL)

if _PG:
    import psycopg2
    import psycopg2.extras

Row = Any  # sqlite3.Row or psycopg2 RealDictRow — both support row["col"] and dict(row)


def get_conn():
    if _PG:
        return psycopg2.connect(
            DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _exec(conn, sql: str, params: tuple = ()):
    """Run one statement on either backend, returning a cursor."""
    if _PG:
        cur = conn.cursor()
        cur.execute(sql.replace("?", "%s"), params)
        return cur
    return conn.execute(sql, params)


_SCHEMA_SQLITE = """
    CREATE TABLE IF NOT EXISTS training (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        date        TEXT NOT NULL,
        venue       TEXT,
        report_time TEXT,
        status      TEXT NOT NULL DEFAULT 'scheduled',
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS attendance (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        training_id INTEGER NOT NULL,
        name        TEXT NOT NULL COLLATE NOCASE,
        status      TEXT NOT NULL DEFAULT 'present',
        late_time   TEXT,
        FOREIGN KEY(training_id) REFERENCES training(id)
    );

    CREATE TABLE IF NOT EXISTS auth (
        user_id  INTEGER PRIMARY KEY,
        username TEXT COLLATE NOCASE,
        role     TEXT NOT NULL CHECK(role IN ('master', 'ic'))
    );

    CREATE TABLE IF NOT EXISTS pending_handover (
        username     TEXT PRIMARY KEY COLLATE NOCASE,
        from_user_id INTEGER NOT NULL,
        from_role    TEXT NOT NULL,
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS name_aliases (
        sheet_name   TEXT PRIMARY KEY COLLATE NOCASE,
        display_name TEXT NOT NULL COLLATE NOCASE
    );

    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
"""

# BIGINT for Telegram user/chat ids — they exceed 32-bit integer range.
_SCHEMA_PG = """
    CREATE TABLE IF NOT EXISTS training (
        id                     SERIAL PRIMARY KEY,
        date                   TEXT NOT NULL,
        venue                  TEXT,
        report_time            TEXT,
        status                 TEXT NOT NULL DEFAULT 'scheduled',
        created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        reminder_chat_id       BIGINT,
        attendance_pos_sent_at TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS attendance (
        id          SERIAL PRIMARY KEY,
        training_id INTEGER NOT NULL REFERENCES training(id),
        name        TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'present',
        late_time   TEXT
    );

    CREATE TABLE IF NOT EXISTS auth (
        user_id  BIGINT PRIMARY KEY,
        username TEXT,
        role     TEXT NOT NULL CHECK(role IN ('master', 'ic'))
    );

    CREATE TABLE IF NOT EXISTS pending_handover (
        username     TEXT PRIMARY KEY,
        from_user_id BIGINT NOT NULL,
        from_role    TEXT NOT NULL,
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS name_aliases (
        sheet_name   TEXT PRIMARY KEY,
        display_name TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
"""


def init_db(master_id: int) -> None:
    """Create all tables and seed the master user. Safe to call on every startup."""
    conn = get_conn()
    if _PG:
        _exec(conn, _SCHEMA_PG)
        _exec(
            conn,
            "INSERT INTO auth (user_id, role) VALUES (?, 'master') "
            "ON CONFLICT (user_id) DO NOTHING",
            (master_id,),
        )
        conn.commit()
        conn.close()
        return

    conn.executescript(_SCHEMA_SQLITE)
    # Add columns that predate this schema (migration for existing local DBs)
    for ddl in (
        "ALTER TABLE training ADD COLUMN reminder_chat_id INTEGER",
        "ALTER TABLE training ADD COLUMN attendance_pos_sent_at TIMESTAMP",
    ):
        try:
            conn.execute(ddl)
            conn.commit()
        except Exception:
            pass  # Column already exists

    conn.execute(
        "INSERT OR IGNORE INTO auth (user_id, role) VALUES (?, 'master')",
        (master_id,),
    )
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────────────────────
# AUTH
# ──────────────────────────────────────────────────────────────

def get_role(user_id: int) -> Optional[str]:
    conn = get_conn()
    row = _exec(conn, "SELECT role FROM auth WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row["role"] if row else None


def is_ic_or_master(user_id: int) -> bool:
    return get_role(user_id) in ("ic", "master")


def list_auth() -> list:
    conn = get_conn()
    rows = _exec(
        conn, "SELECT user_id, username, role FROM auth ORDER BY role DESC"
    ).fetchall()
    conn.close()
    return rows


def set_ic(user_id: int, username: str) -> None:
    conn = get_conn()
    if _PG:
        sql = (
            "INSERT INTO auth (user_id, username, role) VALUES (?, ?, 'ic') "
            "ON CONFLICT (user_id) DO UPDATE "
            "SET username = EXCLUDED.username, role = EXCLUDED.role"
        )
    else:
        sql = "INSERT OR REPLACE INTO auth (user_id, username, role) VALUES (?, ?, 'ic')"
    _exec(conn, sql, (user_id, username.lstrip("@").lower()))
    conn.commit()
    conn.close()


def clear_ics() -> None:
    """Remove all IC-role users (used during handover)."""
    conn = get_conn()
    _exec(conn, "DELETE FROM auth WHERE role = 'ic'")
    conn.commit()
    conn.close()


def remove_ic_by_username(username: str) -> bool:
    conn = get_conn()
    cur = _exec(
        conn,
        "DELETE FROM auth WHERE username = ? AND role = 'ic'",
        (username.lstrip("@").lower(),),
    )
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


def set_pending_handover(username: str, from_user_id: int, from_role: str) -> None:
    conn = get_conn()
    if _PG:
        sql = (
            "INSERT INTO pending_handover (username, from_user_id, from_role) VALUES (?, ?, ?) "
            "ON CONFLICT (username) DO UPDATE SET from_user_id = EXCLUDED.from_user_id, "
            "from_role = EXCLUDED.from_role, created_at = CURRENT_TIMESTAMP"
        )
    else:
        sql = "INSERT OR REPLACE INTO pending_handover (username, from_user_id, from_role) VALUES (?, ?, ?)"
    _exec(conn, sql, (username.lower(), from_user_id, from_role))
    conn.commit()
    conn.close()


def get_pending_handover(username: str) -> Optional[Row]:
    conn = get_conn()
    row = _exec(
        conn, "SELECT * FROM pending_handover WHERE username = ?", (username.lower(),)
    ).fetchone()
    conn.close()
    return row


def delete_pending_handover(username: str) -> None:
    conn = get_conn()
    _exec(conn, "DELETE FROM pending_handover WHERE username = ?", (username.lower(),))
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────────────────────
# TRAINING
# ──────────────────────────────────────────────────────────────

def get_active_training() -> Optional[Row]:
    """Returns the most recently created scheduled training."""
    conn = get_conn()
    row = _exec(
        conn,
        "SELECT * FROM training WHERE status = 'scheduled' ORDER BY id DESC LIMIT 1",
    ).fetchone()
    conn.close()
    return row


def get_training_by_date(date_str: str) -> Optional[Row]:
    """Return the scheduled training record for the given DD/MM/YYYY date, or None."""
    conn = get_conn()
    row = _exec(
        conn,
        "SELECT * FROM training WHERE date = ? AND status = 'scheduled' ORDER BY id DESC LIMIT 1",
        (date_str,),
    ).fetchone()
    conn.close()
    return row


def create_training(date: str, venue: str, report_time: str, reminder_chat_id: int = None) -> int:
    conn = get_conn()
    sql = "INSERT INTO training (date, venue, report_time, reminder_chat_id) VALUES (?, ?, ?, ?)"
    params = (date, venue.upper(), report_time, reminder_chat_id)
    if _PG:
        cur = _exec(conn, sql + " RETURNING id", params)
        training_id = cur.fetchone()["id"]
    else:
        cur = _exec(conn, sql, params)
        training_id = cur.lastrowid
    conn.commit()
    conn.close()
    return training_id


def set_training_reminder_chat(training_id: int, chat_id: int) -> None:
    conn = get_conn()
    _exec(
        conn,
        "UPDATE training SET reminder_chat_id = ? WHERE id = ?",
        (chat_id, training_id),
    )
    conn.commit()
    conn.close()


def mark_attendance_pos_sent(training_id: int) -> None:
    """Record that the day-before position attendance post was delivered."""
    conn = get_conn()
    _exec(
        conn,
        "UPDATE training SET attendance_pos_sent_at = CURRENT_TIMESTAMP WHERE id = ?",
        (training_id,),
    )
    conn.commit()
    conn.close()




def set_attendance(training_id: int, attendees: list[tuple[str, str, Optional[str]]]) -> None:
    """Replace attendance list for a training. Each entry is (name, status, late_time)."""
    conn = get_conn()
    _exec(conn, "DELETE FROM attendance WHERE training_id = ?", (training_id,))
    for name, status, late_time in attendees:
        _exec(
            conn,
            "INSERT INTO attendance (training_id, name, status, late_time) VALUES (?, ?, ?, ?)",
            (training_id, name.lower().strip(), status, late_time),
        )
    conn.commit()
    conn.close()


def get_attendance_rows(training_id: int) -> list:
    conn = get_conn()
    rows = _exec(
        conn,
        "SELECT name, status, late_time FROM attendance WHERE training_id = ?",
        (training_id,),
    ).fetchall()
    conn.close()
    return rows


def purge_old_trainings(days: int = 14) -> int:
    """Delete training sessions (and their attendance/required data) older than `days` days.
    Returns the number of training rows deleted."""
    from datetime import date, timedelta, datetime
    cutoff = date.today() - timedelta(days=days)
    conn = get_conn()
    # Find old training IDs — compare stored DD/MM/YYYY date strings
    rows = _exec(conn, "SELECT id, date FROM training").fetchall()
    old_ids = []
    for row in rows:
        try:
            d = datetime.strptime(row["date"], "%d/%m/%Y").date()
            if d < cutoff:
                old_ids.append(row["id"])
        except ValueError:
            pass
    for tid in old_ids:
        _exec(conn, "DELETE FROM attendance WHERE training_id = ?", (tid,))
        _exec(conn, "DELETE FROM training WHERE id = ?", (tid,))
    conn.commit()
    conn.close()
    return len(old_ids)


# ──────────────────────────────────────────────────────────────
# NAME ALIASES
# ──────────────────────────────────────────────────────────────

def set_name_alias(sheet_name: str, display_name: str) -> None:
    """Map sheet_name → display_name. Overwrites if sheet_name already exists."""
    conn = get_conn()
    _exec(
        conn,
        """INSERT INTO name_aliases (sheet_name, display_name) VALUES (?, ?)
           ON CONFLICT(sheet_name) DO UPDATE SET display_name = excluded.display_name""",
        (sheet_name.lower().strip(), display_name.lower().strip()),
    )
    conn.commit()
    conn.close()


def remove_name_alias(sheet_name: str) -> bool:
    """Remove an alias. Returns True if it existed."""
    conn = get_conn()
    cur = _exec(
        conn,
        "DELETE FROM name_aliases WHERE sheet_name = ?",
        (sheet_name.lower().strip(),),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def get_all_name_aliases() -> dict[str, str]:
    """Return {sheet_name: display_name} for all stored aliases."""
    conn = get_conn()
    rows = _exec(conn, "SELECT sheet_name, display_name FROM name_aliases").fetchall()
    conn.close()
    return {r["sheet_name"]: r["display_name"] for r in rows}


# ──────────────────────────────────────────────────────────────
# SETTINGS
# ──────────────────────────────────────────────────────────────

def set_setting(key: str, value: str) -> None:
    """Store a key/value setting (e.g. the default reminder chat id)."""
    conn = get_conn()
    if _PG:
        sql = (
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
    else:
        sql = "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)"
    _exec(conn, sql, (key, value))
    conn.commit()
    conn.close()


def get_setting(key: str) -> Optional[str]:
    conn = get_conn()
    row = _exec(conn, "SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def clear_active_training() -> bool:
    """Cancel the current scheduled training and wipe its attendance/required data."""
    conn = get_conn()
    row = _exec(
        conn,
        "SELECT id FROM training WHERE status = 'scheduled' ORDER BY id DESC LIMIT 1",
    ).fetchone()
    if not row:
        conn.close()
        return False
    tid = row["id"]
    _exec(conn, "DELETE FROM attendance WHERE training_id = ?", (tid,))
    _exec(conn, "UPDATE training SET status = 'cleared' WHERE id = ?", (tid,))
    conn.commit()
    conn.close()
    return True

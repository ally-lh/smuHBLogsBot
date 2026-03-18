"""
database.py — SQLite layer for smuHBLogs
All DB reads/writes go through here. bot.py never touches sqlite directly.
"""

import sqlite3
import os
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "hblogs.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(master_id: int) -> None:
    """Create all tables and seed the master user. Safe to call on every startup."""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS inventory (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            holder   TEXT NOT NULL COLLATE NOCASE,
            item     TEXT NOT NULL COLLATE NOCASE,
            quantity INTEGER NOT NULL DEFAULT 1,
            UNIQUE(holder, item)
        );

        CREATE TABLE IF NOT EXISTS training (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL,
            venue       TEXT,
            report_time TEXT,
            status      TEXT NOT NULL DEFAULT 'scheduled',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS training_required (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            training_id INTEGER NOT NULL,
            item        TEXT NOT NULL COLLATE NOCASE,
            quantity    INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(training_id) REFERENCES training(id)
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
    """)
    # Add reminder_chat_id column if it doesn't exist yet (migration)
    try:
        conn.execute("ALTER TABLE training ADD COLUMN reminder_chat_id INTEGER")
        conn.commit()
    except Exception:
        pass  # Column already exists

    # Seed master — idempotent, safe to re-run
    conn.execute(
        "INSERT OR IGNORE INTO auth (user_id, role) VALUES (?, 'master')",
        (master_id,)
    )
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────────────────────
# AUTH
# ──────────────────────────────────────────────────────────────

def get_role(user_id: int) -> Optional[str]:
    conn = get_conn()
    row = conn.execute("SELECT role FROM auth WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row["role"] if row else None


def is_ic_or_master(user_id: int) -> bool:
    return get_role(user_id) in ("ic", "master")


def list_auth() -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT user_id, username, role FROM auth ORDER BY role DESC"
    ).fetchall()
    conn.close()
    return rows


def set_ic(user_id: int, username: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO auth (user_id, username, role) VALUES (?, ?, 'ic')",
        (user_id, username.lstrip("@").lower())
    )
    conn.commit()
    conn.close()


def clear_ics() -> None:
    """Remove all IC-role users (used during handover)."""
    conn = get_conn()
    conn.execute("DELETE FROM auth WHERE role = 'ic'")
    conn.commit()
    conn.close()


def remove_ic_by_username(username: str) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM auth WHERE username = ? AND role = 'ic'",
        (username.lstrip("@").lower(),)
    )
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


def set_pending_handover(username: str, from_user_id: int, from_role: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO pending_handover (username, from_user_id, from_role) VALUES (?, ?, ?)",
        (username.lower(), from_user_id, from_role)
    )
    conn.commit()
    conn.close()


def get_pending_handover(username: str) -> Optional[sqlite3.Row]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM pending_handover WHERE username = ?",
        (username.lower(),)
    ).fetchone()
    conn.close()
    return row


def delete_pending_handover(username: str) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM pending_handover WHERE username = ?", (username.lower(),))
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────────────────────
# INVENTORY
# ──────────────────────────────────────────────────────────────

def set_holding(holder: str, item: str, quantity: int = 1) -> None:
    """Set or overwrite a person's holding for an item."""
    conn = get_conn()
    conn.execute(
        """INSERT INTO inventory (holder, item, quantity) VALUES (?, ?, ?)
           ON CONFLICT(holder, item) DO UPDATE SET quantity = excluded.quantity""",
        (holder.lower().strip(), item.lower().strip(), quantity)
    )
    conn.commit()
    conn.close()


def remove_holding(holder: str, item: str) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM inventory WHERE holder = ? AND item = ?",
        (holder.lower().strip(), item.lower().strip())
    )
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


def transfer_item(item: str, from_holder: str, to_holder: str) -> bool:
    """Move an item from one holder to another (adds to existing qty if to_holder already has some)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT quantity FROM inventory WHERE holder = ? AND item = ?",
        (from_holder.lower(), item.lower())
    ).fetchone()
    if not row:
        conn.close()
        return False
    qty = row["quantity"]
    conn.execute(
        "DELETE FROM inventory WHERE holder = ? AND item = ?",
        (from_holder.lower(), item.lower())
    )
    conn.execute(
        """INSERT INTO inventory (holder, item, quantity) VALUES (?, ?, ?)
           ON CONFLICT(holder, item) DO UPDATE SET quantity = quantity + excluded.quantity""",
        (to_holder.lower(), item.lower(), qty)
    )
    conn.commit()
    conn.close()
    return True


def get_full_inventory() -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT holder, item, quantity FROM inventory ORDER BY holder, item"
    ).fetchall()
    conn.close()
    return rows


def search_inventory_by_item(item: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT holder, item, quantity FROM inventory WHERE item LIKE ?",
        (f"%{item.lower()}%",)
    ).fetchall()
    conn.close()
    return rows


def search_inventory_by_holder(holder: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT holder, item, quantity FROM inventory WHERE holder LIKE ?",
        (f"%{holder.lower()}%",)
    ).fetchall()
    conn.close()
    return rows


def rename_holder(old_name: str, new_name: str) -> int:
    """
    Rename a holder across inventory and attendance.
    If new_name already holds some of the same items, quantities are merged.
    Returns the number of inventory rows affected.
    """
    old = old_name.lower().strip()
    new = new_name.lower().strip()
    conn = get_conn()

    rows = conn.execute(
        "SELECT item, quantity FROM inventory WHERE holder = ?", (old,)
    ).fetchall()

    for row in rows:
        conn.execute(
            """INSERT INTO inventory (holder, item, quantity) VALUES (?, ?, ?)
               ON CONFLICT(holder, item) DO UPDATE SET quantity = quantity + excluded.quantity""",
            (new, row["item"], row["quantity"])
        )
    conn.execute("DELETE FROM inventory WHERE holder = ?", (old,))

    # Also rename in any attendance records
    conn.execute(
        "UPDATE attendance SET name = ? WHERE name = ?", (new, old)
    )

    conn.commit()
    conn.close()
    return len(rows)


def get_all_holders() -> list[str]:
    """Return all distinct holder names currently in inventory, sorted."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT holder FROM inventory ORDER BY holder"
    ).fetchall()
    conn.close()
    return [r["holder"] for r in rows]


def clear_inventory() -> None:
    conn = get_conn()
    conn.execute("DELETE FROM inventory")
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────────────────────
# TRAINING
# ──────────────────────────────────────────────────────────────

def get_active_training() -> Optional[sqlite3.Row]:
    """Returns the most recently created scheduled training."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM training WHERE status = 'scheduled' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row


def get_training_by_date(date_str: str) -> Optional[sqlite3.Row]:
    """Return the scheduled training record for the given DD/MM/YYYY date, or None."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM training WHERE date = ? AND status = 'scheduled' ORDER BY rowid DESC LIMIT 1",
        (date_str,)
    ).fetchone()
    conn.close()
    return row


def create_training(date: str, venue: str, report_time: str, reminder_chat_id: int = None) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO training (date, venue, report_time, reminder_chat_id) VALUES (?, ?, ?, ?)",
        (date, venue.upper(), report_time, reminder_chat_id)
    )
    training_id = cur.lastrowid
    conn.commit()
    conn.close()
    return training_id


def set_training_reminder_chat(training_id: int, chat_id: int) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE training SET reminder_chat_id = ? WHERE id = ?",
        (chat_id, training_id)
    )
    conn.commit()
    conn.close()


def set_required_items(training_id: int, items: list[tuple[str, int]]) -> None:
    """Replace all required items for a training session."""
    conn = get_conn()
    conn.execute("DELETE FROM training_required WHERE training_id = ?", (training_id,))
    for item, qty in items:
        conn.execute(
            "INSERT INTO training_required (training_id, item, quantity) VALUES (?, ?, ?)",
            (training_id, item.lower().strip(), qty)
        )
    conn.commit()
    conn.close()


def get_required_items(training_id: int) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT item, quantity FROM training_required WHERE training_id = ?",
        (training_id,)
    ).fetchall()
    conn.close()
    return rows


def set_attendance(training_id: int, attendees: list[tuple[str, str, Optional[str]]]) -> None:
    """Replace attendance list for a training. Each entry is (name, status, late_time)."""
    conn = get_conn()
    conn.execute("DELETE FROM attendance WHERE training_id = ?", (training_id,))
    for name, status, late_time in attendees:
        conn.execute(
            "INSERT INTO attendance (training_id, name, status, late_time) VALUES (?, ?, ?, ?)",
            (training_id, name.lower().strip(), status, late_time)
        )
    conn.commit()
    conn.close()


def get_attendance_rows(training_id: int) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT name, status, late_time FROM attendance WHERE training_id = ?",
        (training_id,)
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
    rows = conn.execute(
        "SELECT id, date FROM training"
    ).fetchall()
    old_ids = []
    for row in rows:
        try:
            d = datetime.strptime(row["date"], "%d/%m/%Y").date()
            if d < cutoff:
                old_ids.append(row["id"])
        except ValueError:
            pass
    for tid in old_ids:
        conn.execute("DELETE FROM attendance WHERE training_id = ?", (tid,))
        conn.execute("DELETE FROM training_required WHERE training_id = ?", (tid,))
        conn.execute("DELETE FROM training WHERE id = ?", (tid,))
    conn.commit()
    conn.close()
    return len(old_ids)


def clear_active_training() -> bool:
    """Cancel the current scheduled training and wipe its attendance/required data."""
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM training WHERE status = 'scheduled' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    if not row:
        conn.close()
        return False
    tid = row["id"]
    conn.execute("DELETE FROM attendance WHERE training_id = ?", (tid,))
    conn.execute("DELETE FROM training_required WHERE training_id = ?", (tid,))
    conn.execute("UPDATE training SET status = 'cleared' WHERE id = ?", (tid,))
    conn.commit()
    conn.close()
    return True

"""
sheets.py — Read-only Google Sheets integration for smuHBLogs.
Reads the team attendance spreadsheet and parses each cell's status.

Sheet layout assumed:
  Row 1 (idx 0): header / instruction text
  Row 2 (idx 1): venue names per session
  Row 3 (idx 2): dates  e.g. "17 Mar, Tues"
  Row 4 (idx 3): warmup / report times
  Row 5+ (idx 4+): player name in col A, attendance values in date columns
"""

import re
import json
import logging
from datetime import date, datetime
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Row indices (0-based)
_VENUE_ROW      = 1
_DATE_ROW       = 2
_TIME_ROW       = 3
_DATA_ROW_START = 4
_NAME_COL       = 0


# ──────────────────────────────────────────────────────────────
# CLIENT
# ──────────────────────────────────────────────────────────────

def _get_client(creds_path: str) -> gspread.Client:
    """
    Load credentials from either:
    - a file path (local dev): SHEET_CREDS=service_account.json
    - raw JSON string (deployment): SHEET_CREDS={"type":"service_account",...}
    """
    try:
        info = json.loads(creds_path)
        creds = Credentials.from_service_account_info(info, scopes=_SCOPES)
    except (json.JSONDecodeError, ValueError):
        creds = Credentials.from_service_account_file(creds_path, scopes=_SCOPES)
    return gspread.authorize(creds)


# ──────────────────────────────────────────────────────────────
# CELL PARSING
# ──────────────────────────────────────────────────────────────

def parse_cell(value: str) -> dict:
    """
    Parse a sheet cell into a structured dict.

    Examples:
      "1"                       → {status: "present"}
      "0"                       → {status: "absent"}
      "0(class)"                → {status: "absent",  reason: "class"}
      "1 (late, work, 7:45)"    → {status: "late",    reason: "work",  eta: "7:45"}
      "1 (late, class eta 730pm)"→ {status: "late",   reason: "class", eta: "730pm"}
      "tbc (submission)"        → {status: "tbc",     reason: "submission"}
      ""                        → {status: "no response"}
    """
    v = value.strip()
    if not v:
        return {"status": "no response"}

    # Extract bracketed note, if any
    note = ""
    note_m = re.search(r'\(([^)]+)\)', v)
    if note_m:
        note = note_m.group(1).strip()

    base = re.sub(r'\s*\([^)]*\)', '', v).strip().lower()

    if base == "1":
        if "late" in note.lower():
            # Extract ETA: "eta 730pm", "eta 8", "7:45", "8.20pm"
            eta_m = re.search(r'eta\s*([\d.:]+(?:pm|am)?)', note, re.IGNORECASE)
            if eta_m:
                eta = eta_m.group(1)
            else:
                bare_m = re.search(
                    r'\b(\d{1,2}[:.]\d{2}(?:pm|am)?|\d{1,2}(?:pm|am))\b',
                    note, re.IGNORECASE,
                )
                eta = bare_m.group(1) if bare_m else None

            # Reason = note parts that aren't "late" and aren't the eta
            parts = [p.strip() for p in note.split(',')]
            reason_parts = []
            for p in parts:
                pl = p.lower()
                if pl == 'late':
                    continue
                if re.match(r'^eta\s*[\d]', pl, re.IGNORECASE):
                    continue
                if eta and re.fullmatch(
                    r'\d{1,2}[:.]\d{2}(?:pm|am)?|\d{1,2}(?:pm|am)',
                    pl, re.IGNORECASE,
                ):
                    continue
                reason_parts.append(p)
            reason = ", ".join(reason_parts) if reason_parts else None
            return {"status": "late", "reason": reason, "eta": eta}

        return {"status": "present", "note": note if note else None}

    elif base == "0":
        return {"status": "absent", "reason": note if note else None}

    elif base == "tbc":
        return {"status": "tbc", "reason": note if note else None}

    else:
        return {"status": "unknown", "raw": v}


def format_cell_status(parsed: dict) -> str:
    """Human-readable one-liner for a parsed cell dict."""
    s = parsed.get("status", "?")
    if s == "present":
        note = parsed.get("note")
        return f"✅ Coming{f' ({note})' if note else ''}"
    if s == "absent":
        reason = parsed.get("reason")
        return f"❌ Absent{f' — {reason}' if reason else ''}"
    if s == "late":
        parts = []
        if parsed.get("reason"):
            parts.append(parsed["reason"])
        if parsed.get("eta"):
            parts.append(f"ETA {parsed['eta']}")
        detail = ", ".join(parts)
        return f"⏰ Late{f' ({detail})' if detail else ''}"
    if s == "tbc":
        reason = parsed.get("reason")
        return f"❓ TBC{f' ({reason})' if reason else ''}"
    if s == "no response":
        return "— No response"
    return f"? {parsed.get('raw', '')}"


# ──────────────────────────────────────────────────────────────
# DATE MATCHING
# ──────────────────────────────────────────────────────────────

def _find_date_column(date_row: list, target: date) -> Optional[int]:
    """
    Find the column index whose header matches target date.
    Matches on day number + month abbreviation only (ignores weekday).
    Handles formats like "17 Mar, Tues", "18 Mar, Wed", "8 Apr, Wed" etc.
    """
    target_day = target.day
    target_month = target.strftime("%b").lower()  # e.g. "mar"

    for i, cell in enumerate(date_row):
        m = re.match(r'^\s*(\d{1,2})\s+([A-Za-z]{3})', cell.strip())
        if m and int(m.group(1)) == target_day and m.group(2).lower() == target_month:
            return i
    return None


# ──────────────────────────────────────────────────────────────
# UPCOMING SESSIONS
# ──────────────────────────────────────────────────────────────

def get_upcoming_sessions(
    spreadsheet_id: str,
    sheet_name: str,
    creds_path: str,
    limit: int = 3,
) -> list[dict]:
    """
    Return the next `limit` training sessions from today onwards.
    Each entry: {"date": date, "venue": str, "time": str}
    """
    client = _get_client(creds_path)
    sheet  = client.open_by_key(spreadsheet_id).worksheet(sheet_name)
    rows   = sheet.get_all_values()

    if len(rows) <= _DATE_ROW:
        return []

    today     = date.today()
    date_row  = rows[_DATE_ROW]
    venue_row = rows[_VENUE_ROW] if len(rows) > _VENUE_ROW else []
    time_row  = rows[_TIME_ROW]  if len(rows) > _TIME_ROW  else []

    sessions = []
    for i, cell in enumerate(date_row):
        m = re.match(r'^\s*(\d{1,2})\s+([A-Za-z]{3})', cell.strip())
        if not m:
            continue
        day   = int(m.group(1))
        month = m.group(2)
        # Try current year, then next year
        for year in (today.year, today.year + 1):
            try:
                d = datetime.strptime(f"{day} {month} {year}", "%d %b %Y").date()
            except ValueError:
                continue
            if d >= today:
                sessions.append({
                    "date":  d,
                    "venue": venue_row[i].strip() if i < len(venue_row) else "",
                    "time":  time_row[i].strip()  if i < len(time_row)  else "",
                })
                break

    sessions.sort(key=lambda s: s["date"])
    return sessions[:limit]


# ──────────────────────────────────────────────────────────────
# MAIN READ FUNCTION
# ──────────────────────────────────────────────────────────────

def get_attendance(
    spreadsheet_id: str,
    sheet_name: str,
    creds_path: str,
    target_date: Optional[date] = None,
) -> Optional[dict]:
    """
    Read attendance for target_date (defaults to today) from the Google Sheet.

    Returns:
      {
        "date":       date object,
        "venue":      str,
        "time":       str,
        "attendance": {name: parsed_cell_dict, ...},
      }
    or None if the date column isn't found in the sheet.
    Raises on connection/auth errors.
    """
    if target_date is None:
        target_date = date.today()

    client = _get_client(creds_path)
    sheet  = client.open_by_key(spreadsheet_id).worksheet(sheet_name)
    rows   = sheet.get_all_values()

    if len(rows) <= _DATE_ROW:
        return None

    col_idx = _find_date_column(rows[_DATE_ROW], target_date)
    if col_idx is None:
        return None

    def _col(row):
        return row[col_idx].strip() if len(row) > col_idx else ""

    venue    = _col(rows[_VENUE_ROW]) if len(rows) > _VENUE_ROW else ""
    time_str = _col(rows[_TIME_ROW])  if len(rows) > _TIME_ROW  else ""

    attendance = {}
    for row in rows[_DATA_ROW_START:]:
        name = row[_NAME_COL].strip() if len(row) > _NAME_COL else ""
        if not name:
            continue
        attendance[name] = parse_cell(_col(row))

    return {
        "date":       target_date,
        "venue":      venue,
        "time":       time_str,
        "attendance": attendance,
    }

"""Reminders management tool."""
from typing import Dict, Any
from app.db import get_db

def send_reminder_internal(message: str, time: str) -> Dict[str, Any]:
    """
    Store reminder in database to alert student.
    """
    if not message.strip():
        raise ValueError("Reminder message cannot be empty")

    with get_db() as conn:
        cur = conn.execute("""
        INSERT INTO reminders (message, remind_at, sent)
        VALUES (?, ?, 0);
        """, (message.strip(), time.strip()))
        r_id = cur.lastrowid

    return {
        "status": "success",
        "reminder_id": r_id,
        "message": message,
        "time": time
    }

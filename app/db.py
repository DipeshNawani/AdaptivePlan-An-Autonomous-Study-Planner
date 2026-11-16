"""Database initialization, connection handling, and schema definitions."""
import sqlite3
import json
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Generator, Any, Dict, Optional
from app.config import DB_PATH, BASE_DIR

def get_connection() -> sqlite3.Connection:
    """Create a SQLite connection with foreign keys and Row factory."""
    conn = sqlite3.connect(str(DB_PATH), timeout=20.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Context manager for database operations with automatic commit/rollback."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS profile (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        max_daily_hours REAL NOT NULL DEFAULT 4.0,
        preferred_time TEXT NOT NULL DEFAULT 'evening',
        break_minutes INTEGER NOT NULL DEFAULT 15,
        burnout_threshold_days INTEGER NOT NULL DEFAULT 5,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS free_slots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        weekday INTEGER NOT NULL, -- 0 = Monday ... 6 = Sunday
        start_time TEXT NOT NULL, -- 'HH:MM'
        end_time TEXT NOT NULL,   -- 'HH:MM'
        energy TEXT NOT NULL DEFAULT 'high' -- 'high' | 'low'
    );

    CREATE TABLE IF NOT EXISTS subjects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        exam_date TEXT NOT NULL, -- 'YYYY-MM-DD'
        priority INTEGER NOT NULL DEFAULT 1,
        color TEXT DEFAULT '#3B82F6'
    );

    CREATE TABLE IF NOT EXISTS topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
        unit TEXT NOT NULL,
        name TEXT NOT NULL,
        difficulty INTEGER NOT NULL DEFAULT 3, -- 1 to 5
        est_hours REAL NOT NULL DEFAULT 2.0,
        weightage REAL NOT NULL DEFAULT 1.0,  -- paper weightage
        mastery REAL NOT NULL DEFAULT 0.0,    -- 0.0 to 1.0
        status TEXT NOT NULL DEFAULT 'pending' -- 'pending', 'in_progress', 'completed'
    );

    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
        type TEXT NOT NULL DEFAULT 'study', -- 'study', 'revision', 'quiz'
        date TEXT NOT NULL, -- 'YYYY-MM-DD'
        start_time TEXT NOT NULL, -- 'HH:MM'
        end_time TEXT NOT NULL,   -- 'HH:MM'
        duration_hours REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', -- 'pending', 'done', 'missed', 'needs_revision'
        reason TEXT,
        priority_score REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS quiz_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
        score REAL NOT NULL, -- 0 to 100
        total_questions INTEGER NOT NULL,
        correct_answers INTEGER NOT NULL,
        taken_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS agent_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        step INTEGER NOT NULL,
        tool_name TEXT NOT NULL,
        args_json TEXT NOT NULL,
        result_json TEXT NOT NULL,
        thought TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS agent_runs (
        id TEXT PRIMARY KEY,                   -- run_id (UUID)
        provider TEXT NOT NULL DEFAULT 'Mock',  -- e.g. 'Gemini (gemini-3.1-flash-lite)' or 'Mock LLM Provider'
        mode TEXT NOT NULL DEFAULT 'mock',      -- 'mock' or 'live'
        status TEXT NOT NULL DEFAULT 'running', -- 'running', 'completed', 'failed'
        iterations INTEGER NOT NULL DEFAULT 0,
        summary TEXT,
        is_fallback INTEGER NOT NULL DEFAULT 0,
        events_json TEXT,                       -- full JSON array of SSE events (persisted on completion)
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        finished_at TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_agent_runs_started ON agent_runs(started_at);

    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT NOT NULL,
        remind_at TEXT NOT NULL,
        sent INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    -- Performance Indexes
    CREATE INDEX IF NOT EXISTS idx_tasks_date ON tasks(date);
    CREATE INDEX IF NOT EXISTS idx_tasks_topic ON tasks(topic_id);
    CREATE INDEX IF NOT EXISTS idx_topics_subject ON topics(subject_id);
    CREATE INDEX IF NOT EXISTS idx_agent_logs_run ON agent_logs(run_id);
    """)

    # Ensure default profile exists
    existing = conn.execute("SELECT id FROM profile WHERE id = 1;").fetchone()
    if not existing:
        conn.execute("""
        INSERT INTO profile (id, max_daily_hours, preferred_time, break_minutes)
        VALUES (1, 4.0, 'evening', 15);
        """)

def init_db() -> None:
    """Initialize database tables and indexes with automatic corruption recovery."""
    try:
        with get_db() as conn:
            _create_tables(conn)
    except sqlite3.DatabaseError as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"Corrupted or invalid SQLite database detected ({e}). Recreating database file at {DB_PATH}")
        if DB_PATH.exists():
            corrupted_path = DB_PATH.with_suffix(".db.corrupted")
            try:
                if corrupted_path.exists():
                    corrupted_path.unlink()
                DB_PATH.rename(corrupted_path)
            except Exception:
                DB_PATH.unlink(missing_ok=True)
        with get_db() as conn:
            _create_tables(conn)


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Retrieve an application setting value from SQLite."""
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?;", (key,)).fetchone()
        return row["value"] if row else default

def set_setting(key: str, value: str) -> None:
    """Store or update an application setting value in SQLite."""
    with get_db() as conn:
        conn.execute("""
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value;
        """, (key, value))

def get_current_llm_mode() -> str:
    """
    Get current LLM mode ('mock' or 'live').
    Default: Live if a GEMINI_API_KEY exists, else Mock. MOCK_LLM in .env only sets this default.
    Persisted in SQLite so it survives restarts.
    """
    mode = get_setting("llm_mode")
    if mode in ("mock", "live"):
        return mode

    # Determine initial default
    from app.config import GEMINI_API_KEY, MOCK_LLM
    if GEMINI_API_KEY and not MOCK_LLM:
        default_mode = "live"
    else:
        default_mode = "mock"

    set_setting("llm_mode", default_mode)
    return default_mode

def set_current_llm_mode(mode: str) -> None:
    """Update current LLM mode ('mock' or 'live') in SQLite."""
    if mode not in ("mock", "live"):
        raise ValueError(f"Invalid mode '{mode}'. Must be 'mock' or 'live'.")

    from app.config import GEMINI_API_KEY
    if mode == "live" and not GEMINI_API_KEY:
        raise ValueError("Add GEMINI_API_KEY to .env to enable Live mode")

    set_setting("llm_mode", mode)

def get_is_demo() -> bool:
    """Check if demo student data is currently loaded."""
    val = get_setting("is_demo", "0")
    return val == "1"

def set_is_demo(is_demo: bool) -> None:
    """Set the is_demo flag in SQLite."""
    set_setting("is_demo", "1" if is_demo else "0")

def reset_to_new_plan() -> None:
    """
    Clear all current subjects, topics, tasks, quiz results, agent logs, and reminders.
    Sets is_demo = False.
    """
    with get_db() as conn:
        conn.execute("DELETE FROM tasks;")
        conn.execute("DELETE FROM quiz_results;")
        conn.execute("DELETE FROM topics;")
        conn.execute("DELETE FROM subjects;")
        conn.execute("DELETE FROM agent_logs;")
        conn.execute("DELETE FROM agent_runs;")
        conn.execute("DELETE FROM reminders;")
    set_is_demo(False)

def seed_demo_data(force: bool = False) -> Dict[str, Any]:
    """Seed demo student data with relative future exam dates."""
    seed_file = BASE_DIR / "data" / "demo_seed.json"
    if not seed_file.exists():
        raise FileNotFoundError(f"Demo seed file not found: {seed_file}")

    with open(seed_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    with get_db() as conn:
        if not force:
            subj_count = conn.execute("SELECT COUNT(*) as c FROM subjects;").fetchone()["c"]
            if subj_count > 0:
                return {"status": "skipped", "message": "Database already contains subjects"}

        # Clear existing tables for fresh demo seed
        conn.execute("DELETE FROM tasks;")
        conn.execute("DELETE FROM quiz_results;")
        conn.execute("DELETE FROM topics;")
        conn.execute("DELETE FROM subjects;")
        conn.execute("DELETE FROM free_slots;")
        conn.execute("DELETE FROM reminders;")
        conn.execute("DELETE FROM agent_logs;")
        conn.execute("DELETE FROM agent_runs;")

        # Update profile
        prof = data.get("profile", {})
        conn.execute("""
        INSERT OR REPLACE INTO profile (id, max_daily_hours, preferred_time, break_minutes)
        VALUES (1, ?, ?, ?);
        """, (prof.get("max_daily_hours", 4.0), prof.get("preferred_time", "evening"), prof.get("break_minutes", 15)))

        # Insert free slots
        for slot in data.get("free_slots", []):
            conn.execute("""
            INSERT INTO free_slots (weekday, start_time, end_time, energy)
            VALUES (?, ?, ?, ?);
            """, (slot["weekday"], slot["start_time"], slot["end_time"], slot.get("energy", "high")))

        today = date.today()
        # Insert subjects and topics with relative dates
        for subj in data.get("subjects", []):
            days_offset = subj.get("days_from_today", 21)
            exam_date = (today + timedelta(days=days_offset)).isoformat()
            cursor = conn.execute("""
            INSERT INTO subjects (name, exam_date, priority, color)
            VALUES (?, ?, ?, ?);
            """, (subj["name"], exam_date, subj.get("priority", 1), subj.get("color", "#3B82F6")))
            subject_id = cursor.lastrowid

            for topic in subj.get("topics", []):
                conn.execute("""
                INSERT INTO topics (subject_id, unit, name, difficulty, est_hours, weightage, mastery, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    subject_id,
                    topic["unit"],
                    topic["name"],
                    topic.get("difficulty", 3),
                    topic.get("est_hours", 2.0),
                    topic.get("weightage", 1.0),
                    topic.get("mastery", 0.0),
                    topic.get("status", "pending")
                ))

    set_is_demo(True)
    return {"status": "success", "message": "Demo data seeded successfully with relative exam dates"}


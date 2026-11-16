"""Unit tests for dynamic replanning, completed task preservation, and panic mode."""
import pytest
from datetime import date, timedelta
from app.db import init_db, seed_demo_data, get_db, get_connection
from app.tools.scheduling import (
    create_schedule_internal, replan_internal, compress_plan_internal
)

@pytest.fixture(autouse=True)
def setup_test_db():
    """Initialize fresh test database with demo data before each test."""
    init_db()
    seed_demo_data(force=True)

def test_replan_preserves_completed_tasks():
    """Verify that replanning preserves all tasks marked as 'done'."""
    today = date.today()
    start_str = today.isoformat()
    end_str = (today + timedelta(days=21)).isoformat()

    create_schedule_internal(start_str, end_str)

    # Mark first 2 tasks as done
    with get_db() as conn:
        done_ids = [r["id"] for r in conn.execute("SELECT id FROM tasks ORDER BY date, start_time LIMIT 2;").fetchall()]
        for tid in done_ids:
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?;", (tid,))

    # Trigger replan from today
    replan_res = replan_internal(from_date=start_str, reason="Testing preservation")
    assert replan_res["status"] == "success"
    assert replan_res["preserved_completed"] == 2

    # Verify these tasks are still marked 'done'
    with get_connection() as conn:
        for tid in done_ids:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?;", (tid,)).fetchone()
            assert row is not None
            assert row["status"] == "done"

def test_replan_after_missed_task():
    """Verify that marking tasks as 'missed' redistributes them and updates delta log."""
    today = date.today()
    start_str = today.isoformat()
    end_str = (today + timedelta(days=21)).isoformat()

    create_schedule_internal(start_str, end_str)

    # Mark a task as missed
    with get_db() as conn:
        task = conn.execute("SELECT id, topic_id FROM tasks WHERE status = 'pending' LIMIT 1;").fetchone()
        assert task is not None
        missed_id = task["id"]
        topic_id = task["topic_id"]
        conn.execute("UPDATE tasks SET status = 'missed' WHERE id = ?;", (missed_id,))

    # Trigger replan
    replan_res = replan_internal(from_date=start_str, reason="Student missed session")
    assert replan_res["status"] == "success"
    assert replan_res["tasks_rescheduled"] >= 1
    assert any("Student missed session" in ch for ch in replan_res["changes"])

    # Verify the topic still has future pending/scheduled tasks
    with get_connection() as conn:
        future_tasks = conn.execute("""
        SELECT COUNT(*) as c FROM tasks
        WHERE topic_id = ? AND date >= ?;
        """, (topic_id, start_str)).fetchone()["c"]
        assert future_tasks >= 1

def test_panic_mode_compression():
    """
    User Point: Extra test covering Panic Mode.
    Verifies that Panic Mode prunes low-yield topics and preserves high-yield ones.
    """
    with get_connection() as conn:
        subj = conn.execute("SELECT id, name FROM subjects LIMIT 1;").fetchone()
        subject_id = subj["id"]

    # Run panic mode for this subject with 3 days left
    res = compress_plan_internal(subject_id=subject_id, days_left=3)
    assert res["compressed"] is True
    assert res["kept_count"] > 0
    assert len(res["kept_topics"]) == res["kept_count"]
    assert "explanation" in res
    assert "Panic Mode activated" in res["explanation"]

    # Verify that kept topics have high weightage compared to dropped topics
    if res["dropped_count"] > 0:
        min_kept_weight = min(t["weightage"] for t in res["kept_topics"])
        max_dropped_weight = max(t["weightage"] for t in res["dropped_topics"])
        assert min_kept_weight >= max_dropped_weight or any(t["difficulty"] >= 4 for t in res["kept_topics"])

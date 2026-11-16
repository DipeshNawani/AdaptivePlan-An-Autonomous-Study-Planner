from starlette.testclient import TestClient
from app.main import app
from app.db import init_db, seed_demo_data, get_connection
from app.tools.progress import mark_progress_internal

def test_mark_missed_counts_on_dashboard():
    init_db()
    seed_demo_data(force=True)
    from datetime import date, timedelta
    from app.tools.scheduling import create_schedule_internal
    today_str = date.today().isoformat()
    end_str = (date.today() + timedelta(days=35)).isoformat()
    create_schedule_internal(start_date=today_str, end_date=end_str)

    client = TestClient(app)

    # 1. Verify initially 0 missed tasks
    dash0 = client.get("/api/dashboard").json()
    assert dash0["summary"]["missed_tasks"] == 0

    # 2. Pick a task scheduled for today or earliest date
    with get_connection() as conn:
        task = conn.execute("SELECT id, date, topic_id FROM tasks WHERE status = 'pending' ORDER BY date ASC LIMIT 1;").fetchone()
        task_id = task["id"]

    # 3. Mark as missed
    res = mark_progress_internal(task_id=task_id, status="missed")
    assert res["status"] == "success"
    assert res["auto_replanned"] is True

    # 4. Check dashboard
    dash1 = client.get("/api/dashboard").json()
    # Missed tasks must be 1 on dashboard
    assert dash1["summary"]["missed_tasks"] >= 1, f"Expected missed_tasks >= 1, got {dash1['summary']['missed_tasks']}"

    # 5. Check database directly
    with get_connection() as conn:
        missed_rows = conn.execute("SELECT id, status, date FROM tasks WHERE status = 'missed';").fetchall()
        assert len(missed_rows) >= 1
        # The specific task must retain status = 'missed'
        marked_task = conn.execute("SELECT status FROM tasks WHERE id = ?;", (task_id,)).fetchone()
        assert marked_task["status"] == "missed"

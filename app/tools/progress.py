"""Progress tracking and spaced repetition revision engine."""
from datetime import date, timedelta
from typing import Dict, Any
from app.db import get_db
from app.tools.scheduling import replan_internal

def add_revision_sessions_internal(task_id: Any = None, subject_id: Any = None) -> Dict[str, Any]:
    """
    Spaced repetition & final revision buffer engine:
    1. If task_id provided: adds spaced repetition revisions (+1, +3, +7 days).
    2. If subject_id provided or task_id is None: adds final pre-exam revision sessions for subjects in their 48h buffer.
    """
    sessions_added = []

    with get_db() as conn:
        if task_id:
            task_info = conn.execute("""
            SELECT t.id, t.topic_id, t.date, t.start_time, t.end_time,
                   tp.name as topic_name, s.exam_date, s.name as subject_name
            FROM tasks t
            JOIN topics tp ON t.topic_id = tp.id
            JOIN subjects s ON tp.subject_id = s.id
            WHERE t.id = ?;
            """, (task_id,)).fetchone()

            if not task_info:
                raise ValueError(f"Task #{task_id} not found")

            base_d = date.fromisoformat(task_info["date"])
            exam_d = date.fromisoformat(task_info["exam_date"])
            topic_id = task_info["topic_id"]

            revision_intervals = [1, 3, 7]
            for offset in revision_intervals:
                rev_date = base_d + timedelta(days=offset)
                if rev_date < exam_d:
                    rev_date_str = rev_date.isoformat()
                    reason = f"Spaced Repetition (+{offset}d) for {task_info['topic_name']}"
                    cur = conn.execute("""
                    INSERT INTO tasks (topic_id, type, date, start_time, end_time, duration_hours, status, reason, priority_score)
                    VALUES (?, 'revision', ?, '19:00', '20:00', 1.0, 'pending', ?, 2.5);
                    """, (topic_id, rev_date_str, reason))
                    sessions_added.append({
                        "task_id": cur.lastrowid,
                        "date": rev_date_str,
                        "interval": f"+{offset}d"
                    })

            return {
                "status": "success",
                "topic_name": task_info["topic_name"],
                "revision_sessions_created": len(sessions_added),
                "sessions": sessions_added
            }

        # Otherwise: add pre-exam revision sessions for subjects lacking them
        query = "SELECT id, name, exam_date FROM subjects"
        params = []
        if subject_id:
            query += " WHERE id = ?"
            params.append(subject_id)
        subjects = conn.execute(query, params).fetchall()

        for subj in subjects:
            s_exam_d = date.fromisoformat(subj["exam_date"])
            buffer_start = (s_exam_d - timedelta(days=2)).isoformat()

            # Check if revision already exists in 48h buffer
            rev_cnt = conn.execute("""
            SELECT COUNT(*) as c FROM tasks t
            JOIN topics tp ON t.topic_id = tp.id
            WHERE tp.subject_id = ? AND t.type = 'revision' AND t.date >= ? AND t.date < ?;
            """, (subj["id"], buffer_start, subj["exam_date"])).fetchone()["c"]

            if rev_cnt == 0:
                # Add revision session on buffer day
                target_date = max(date.today(), s_exam_d - timedelta(days=1)).isoformat()
                top = conn.execute("""
                SELECT id, name FROM topics WHERE subject_id = ? ORDER BY weightage DESC, mastery ASC LIMIT 1;
                """, (subj["id"],)).fetchone()
                if top:
                    cur = conn.execute("""
                    INSERT INTO tasks (topic_id, type, date, start_time, end_time, duration_hours, status, reason, priority_score)
                    VALUES (?, 'revision', ?, '18:00', '19:00', 1.0, 'pending', ?, 3.5);
                    """, (top["id"], target_date, f"Final 48h revision buffer for {subj['name']} ({top['name']})"))
                    sessions_added.append({
                        "task_id": cur.lastrowid,
                        "subject": subj["name"],
                        "date": target_date
                    })

    return {
        "status": "success",
        "revision_sessions_created": len(sessions_added),
        "sessions": sessions_added
    }

def mark_progress_internal(task_id: int, status: str) -> Dict[str, Any]:
    """
    Update study task progress.
    - If status == 'done': marks topic completed and automatically schedules spaced repetition.
    - If status == 'missed': automatically triggers dynamic replanning.
    - If status == 'needs_revision': adds extra revision review.
    """
    valid_statuses = ("done", "missed", "needs_revision")
    if status not in valid_statuses:
        raise ValueError(f"Invalid status '{status}'. Must be one of {valid_statuses}")

    replan_result = None
    revision_result = None

    with get_db() as conn:
        task = conn.execute("SELECT id, topic_id, date FROM tasks WHERE id = ?;", (task_id,)).fetchone()
        if not task:
            raise ValueError(f"Task #{task_id} not found")

        conn.execute("UPDATE tasks SET status = ? WHERE id = ?;", (status, task_id))

        if status == "done":
            # Update topic status and mastery
            conn.execute("UPDATE topics SET status = 'completed', mastery = MAX(mastery, 0.7) WHERE id = ?;", (task["topic_id"],))

    # Automatic side effects
    if status == "done":
        revision_result = add_revision_sessions_internal(task_id)
    elif status == "missed":
        replan_result = replan_internal(from_date=task["date"], reason=f"Task #{task_id} marked as missed")
    elif status == "needs_revision":
        with get_db() as conn:
            conn.execute("UPDATE topics SET mastery = MAX(0.2, mastery - 0.2) WHERE id = ?;", (task["topic_id"],))
        revision_result = add_revision_sessions_internal(task_id)

    return {
        "status": "success",
        "task_id": task_id,
        "new_status": status,
        "revision_added": revision_result,
        "auto_replanned": replan_result is not None,
        "replan_details": replan_result
    }

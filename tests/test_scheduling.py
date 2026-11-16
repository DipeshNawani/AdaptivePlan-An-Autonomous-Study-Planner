"""Unit tests for deterministic scheduling engine, priority math, buffers, and tool validation."""
import pytest
from datetime import date, timedelta
from app.db import init_db, seed_demo_data, get_db, get_connection
from app.models import (
    FreeSlotModel, GetFreeSlotsToolArgs, MarkProgressToolArgs, CompressPlanToolArgs
)
from app.tools.scheduling import (
    calculate_priority, create_schedule_internal, check_conflicts_internal
)

@pytest.fixture(autouse=True)
def setup_test_db():
    """Initialize fresh test database with demo data before each test."""
    init_db()
    seed_demo_data(force=True)

def test_priority_formula():
    """Verify deterministic priority score calculation."""
    # days=10, diff=4, weight=1.5, mastery=0.2 => deficit = 1 + (1 - 0.2) = 1.8
    # score = (1/10) * 4 * 1.5 * 1.8 = 0.1 * 6.0 * 1.8 = 1.08
    score = calculate_priority(days_until_exam=10, difficulty=4, weightage=1.5, mastery=0.2)
    assert score == 1.08

    # Edge case: days_until_exam = 0 (same day) should not divide by zero
    zero_day_score = calculate_priority(days_until_exam=0, difficulty=5, weightage=2.0, mastery=0.0)
    assert zero_day_score == 20.0

def test_max_daily_hours_enforced():
    """Verify that no day has more scheduled hours than profile.max_daily_hours."""
    today = date.today()
    start_str = today.isoformat()
    end_str = (today + timedelta(days=21)).isoformat()

    with get_connection() as conn:
        profile = conn.execute("SELECT max_daily_hours FROM profile WHERE id = 1;").fetchone()
        max_daily_hours = profile["max_daily_hours"]

    res = create_schedule_internal(start_str, end_str)
    assert res["status"] == "success"
    assert res["tasks_created"] > 0

    with get_connection() as conn:
        daily_sums = conn.execute("""
        SELECT date, SUM(duration_hours) as total
        FROM tasks
        GROUP BY date;
        """).fetchall()

        for day in daily_sums:
            # Allow epsilon of 0.01 for floating point rounding
            assert day["total"] <= max_daily_hours + 0.01, f"Day {day['date']} exceeded max hours ({day['total']} > {max_daily_hours})"

def test_short_notice_exam_fallback():
    """Verify short-notice exam (<= 2 days) triggers warning and revision-only mode."""
    today = date.today()
    tomorrow = today + timedelta(days=1)

    # Insert a short-notice subject
    with get_db() as conn:
        cursor = conn.execute("""
        INSERT INTO subjects (name, exam_date, priority, color)
        VALUES ('Emergency Short Notice Exam', ?, 1, '#EF4444');
        """, (tomorrow.isoformat(),))
        s_id = cursor.lastrowid
        conn.execute("""
        INSERT INTO topics (subject_id, unit, name, difficulty, est_hours, weightage, mastery, status)
        VALUES (?, 'Unit 1', 'Rapid Topic Review', 4, 2.0, 1.5, 0.1, 'pending');
        """, (s_id,))

    res = create_schedule_internal(today.isoformat(), (today + timedelta(days=7)).isoformat())
    assert res["status"] == "success"
    # Verify short notice warning was emitted
    has_warning = any("Short-notice exam alert" in w for w in res["warnings"])
    assert has_warning, "Expected short-notice warning for exam 1 day away"

def test_conflict_detection_catches_overlaps():
    """Verify check_conflicts_internal detects artificially injected overlaps and overloads."""
    today = date.today().isoformat()
    # Inject two overlapping tasks on the same day
    with get_db() as conn:
        conn.execute("DELETE FROM tasks;")
        t_id = conn.execute("SELECT id FROM topics LIMIT 1;").fetchone()["id"]
        conn.execute("""
        INSERT INTO tasks (topic_id, type, date, start_time, end_time, duration_hours, status)
        VALUES (?, 'study', ?, '18:00', '20:00', 2.0, 'pending');
        """, (t_id, today))
        conn.execute("""
        INSERT INTO tasks (topic_id, type, date, start_time, end_time, duration_hours, status)
        VALUES (?, 'study', ?, '19:00', '21:00', 2.0, 'pending');
        """, (t_id, today))

    conflicts_res = check_conflicts_internal()
    assert conflicts_res["conflicts_found"] is True
    overlap_types = [c["type"] for c in conflicts_res["conflicts"]]
    assert "time_overlap" in overlap_types

def test_tool_arguments_validation():
    """
    User Point: Extra test checking tool arguments validation.
    Verifies that the LLM cannot pass invalid dates, negative hours, or bad ranges.
    """
    # 1. Invalid date pattern
    with pytest.raises(Exception):
        GetFreeSlotsToolArgs(start_date="not-a-date", end_date="2026-11-20")

    # 2. Negative or zero topic_id
    with pytest.raises(Exception):
        MarkProgressToolArgs(task_id=-5, status="done")

    # 3. Invalid status string
    with pytest.raises(Exception):
        MarkProgressToolArgs(task_id=1, status="invalid_status")

    # 4. Out-of-bounds days_left in Panic Mode
    with pytest.raises(Exception):
        CompressPlanToolArgs(subject_id=1, days_left=0)

    with pytest.raises(Exception):
        CompressPlanToolArgs(subject_id=1, days_left=100)

    # 5. Invalid FreeSlot end_time <= start_time
    with pytest.raises(Exception):
        FreeSlotModel(weekday=1, start_time="20:00", end_time="18:00")

def test_slots_are_filled_back_to_back():
    """Verify scheduler fills slots with multiple tasks back-to-back instead of wasting capacity."""
    today = date.today()
    start_str = today.isoformat()
    end_str = (today + timedelta(days=21)).isoformat()

    # Set up a wide 4h slot on weekdays (18:00 - 22:00) with max_daily_hours = 4.0
    with get_db() as conn:
        conn.execute("UPDATE profile SET max_daily_hours = 4.0, break_minutes = 15 WHERE id = 1;")
        conn.execute("DELETE FROM free_slots;")
        for day_idx in range(7):
            conn.execute("""
            INSERT INTO free_slots (weekday, start_time, end_time, energy)
            VALUES (?, '18:00', '22:00', 'high');
            """, (day_idx,))

    res = create_schedule_internal(start_str, end_str)
    assert res["status"] == "success"

    with get_connection() as conn:
        # Check task count per day where study occurred
        tasks_per_day = conn.execute("""
        SELECT date, COUNT(*) as c, SUM(duration_hours) as total
        FROM tasks
        GROUP BY date
        HAVING total >= 2.5;
        """).fetchall()

        # At least some days should contain multiple tasks placed back-to-back in the 4h window
        has_multiple_in_day = any(r["c"] >= 2 for r in tasks_per_day)
        assert has_multiple_in_day, "Expected at least 2 tasks placed back-to-back in a 4-hour window"

def test_final_2_days_revision_exists():
    """Verify that in the last 2 days before each exam, pre-exam revision tasks exist."""
    today = date.today()
    start_str = today.isoformat()
    end_str = (today + timedelta(days=28)).isoformat()

    res = create_schedule_internal(start_str, end_str)
    assert res["status"] == "success"

    with get_connection() as conn:
        subjects = conn.execute("SELECT id, name, exam_date FROM subjects;").fetchall()
        for subj in subjects:
            exam_d = date.fromisoformat(subj["exam_date"])
            buffer_start = (exam_d - timedelta(days=2)).isoformat()
            rev_cnt = conn.execute("""
            SELECT COUNT(*) as c
            FROM tasks t
            JOIN topics tp ON t.topic_id = tp.id
            WHERE tp.subject_id = ? AND t.type = 'revision' AND t.date >= ? AND t.date < ?;
            """, (subj["id"], buffer_start, subj["exam_date"])).fetchone()["c"]

            assert rev_cnt > 0, f"Expected revision tasks in 48h buffer before exam for {subj['name']}"

    # Conflict auditor must report 0 missing_final_revision conflicts
    audit = check_conflicts_internal()
    missing_rev = [c for c in audit["conflicts"] if c["type"] == "missing_final_revision"]
    assert len(missing_rev) == 0, f"Unexpected missing_final_revision conflict: {missing_rev}"

def test_no_unscheduled_topics_when_capacity_allows():
    """Verify no unscheduled topics when student has ample hours and days."""
    today = date.today()
    start_str = today.isoformat()
    end_str = (today + timedelta(days=35)).isoformat()

    with get_db() as conn:
        conn.execute("UPDATE profile SET max_daily_hours = 5.0 WHERE id = 1;")

    res = create_schedule_internal(start_str, end_str)
    assert res["status"] == "success"
    assert res["unscheduled_topics"] == []
    assert res["unscheduled_hours"] == 0.0

    audit = check_conflicts_internal()
    unsched_conflicts = [c for c in audit["conflicts"] if c["type"] == "unscheduled_topics"]
    assert len(unsched_conflicts) == 0

def test_unscheduled_warning_when_capacity_too_low():
    """Verify unscheduled topics conflict appears when capacity is constrained."""
    today = date.today()
    start_str = today.isoformat()
    end_str = (today + timedelta(days=7)).isoformat()

    # Constrain daily hours to only 0.5h/day
    with get_db() as conn:
        conn.execute("UPDATE profile SET max_daily_hours = 0.5 WHERE id = 1;")
        # Set all exams to 3 days away
        three_days = (today + timedelta(days=3)).isoformat()
        conn.execute("UPDATE subjects SET exam_date = ?;", (three_days,))

    res = create_schedule_internal(start_str, end_str)
    assert res["status"] == "success"
    assert len(res["unscheduled_topics"]) > 0
    assert res["unscheduled_hours"] > 0

    audit = check_conflicts_internal()
    assert audit["conflicts_found"] is True
    unsched = [c for c in audit["conflicts"] if c["type"] == "unscheduled_topics"]
    assert len(unsched) == 1
    assert "could not fit before the exam" in unsched[0]["message"]

@pytest.mark.anyio
async def test_self_correction_loop_ends_with_zero_conflicts():
    """Verify PlannerAgent self-correction loop resolves conflicts and verifies 0 conflicts."""
    from app.agents.planner import PlannerAgent
    from app.db import set_current_llm_mode

    set_current_llm_mode("mock")

    with get_db() as conn:
        conn.execute("UPDATE profile SET max_daily_hours = 4.0 WHERE id = 1;")

    agent = PlannerAgent()
    result = await agent.run()

    assert result["status"] == "success"
    assert result["conflicts_resolved"] is True
    assert "validated" in result["summary"].lower() or "zero conflicts" in result["summary"].lower()

def test_subject_with_6_topics_gets_6_revision_tasks_in_last_2_days():
    """
    A subject with 6 topics and roomy capacity gets 6 revision tasks in its last 2 days
    and no unscheduled conflict.
    """
    today = date.today()
    start_str = today.isoformat()
    end_str = (today + timedelta(days=10)).isoformat()
    # Exam is 5 days away (so Days 3 and 4 are the last 2 days before the exam)
    exam_str = (today + timedelta(days=5)).isoformat()

    with get_db() as conn:
        conn.execute("DELETE FROM tasks;")
        conn.execute("DELETE FROM topics;")
        conn.execute("DELETE FROM subjects;")

        # Roomy capacity: 8 hours per day
        conn.execute("UPDATE profile SET max_daily_hours = 8.0, break_minutes = 15 WHERE id = 1;")

        # Free slots: 09:00 - 18:00 (9 hours available daily)
        conn.execute("DELETE FROM free_slots;")
        for day_idx in range(7):
            conn.execute("""
            INSERT INTO free_slots (weekday, start_time, end_time, energy)
            VALUES (?, '09:00', '18:00', 'high');
            """, (day_idx,))

        cur = conn.execute("""
        INSERT INTO subjects (name, exam_date, priority, color)
        VALUES ('Distributed Systems', ?, 1, '#3B82F6');
        """, (exam_str,))
        subj_id = cur.lastrowid

        # Insert 6 topics (1.0h study each)
        for i in range(1, 7):
            conn.execute("""
            INSERT INTO topics (subject_id, unit, name, difficulty, est_hours, weightage, mastery, status)
            VALUES (?, 'Unit 1', ?, 3, 1.0, ?, ?, 'not_started');
            """, (subj_id, f"Topic {i}", 1.0 + (i * 0.1), 0.3 + (i * 0.05)))

    res = create_schedule_internal(start_str, end_str)
    assert res["status"] == "success"
    assert len(res["unscheduled_topics"]) == 0
    assert res["unscheduled_hours"] == 0.0

    # Conflict audit: 0 conflicts
    audit = check_conflicts_internal()
    assert audit["conflicts_found"] is False
    assert len(audit["conflicts"]) == 0

    # Verify that in the last 2 days before the exam, exactly 6 revision tasks exist (1 per topic, max 1h each)
    exam_d = date.fromisoformat(exam_str)
    buffer_start = (exam_d - timedelta(days=2)).isoformat()

    with get_connection() as conn:
        rev_tasks = conn.execute("""
        SELECT topic_id, duration_hours, type, date
        FROM tasks
        WHERE type = 'revision' AND date >= ? AND date < ? AND status != 'missed';
        """, (buffer_start, exam_str)).fetchall()

        assert len(rev_tasks) == 6, f"Expected 6 revision tasks in last 2 days, got {len(rev_tasks)}"
        # All 6 topics are distinct
        distinct_topic_ids = {r["topic_id"] for r in rev_tasks}
        assert len(distinct_topic_ids) == 6, "Expected each of the 6 topics to have a revision task"
        # Each revision task is max 1h
        for r in rev_tasks:
            assert r["duration_hours"] <= 1.0, f"Revision task duration {r['duration_hours']} exceeds 1.0h"


def test_short_notice_subject_2_days_3_topics_zero_conflicts():
    """
    A subject with an exam in 2 days and 3 topics (6h total) gives zero
    conflicts and zero unscheduled topics when capacity allows.  Every task
    for that subject must be type 'revision' (never 'study') so
    revision_buffer_violation is never triggered.
    """
    today = date.today()
    start_str = today.isoformat()
    end_str = (today + timedelta(days=5)).isoformat()
    # Exam is exactly 2 days away
    exam_str = (today + timedelta(days=2)).isoformat()

    with get_db() as conn:
        conn.execute("DELETE FROM tasks;")
        conn.execute("DELETE FROM topics;")
        conn.execute("DELETE FROM subjects;")

        # Roomy capacity: 8 hours per day
        conn.execute("UPDATE profile SET max_daily_hours = 8.0, break_minutes = 15 WHERE id = 1;")

        # Free slots: 08:00 - 22:00 (14 hours available daily)
        conn.execute("DELETE FROM free_slots;")
        for day_idx in range(7):
            conn.execute("""
            INSERT INTO free_slots (weekday, start_time, end_time, energy)
            VALUES (?, '08:00', '22:00', 'high');
            """, (day_idx,))

        cur = conn.execute("""
        INSERT INTO subjects (name, exam_date, priority, color)
        VALUES ('Short Notice Subject', ?, 1, '#EF4444');
        """, (exam_str,))
        subj_id = cur.lastrowid

        # 3 topics: 2h each = 6h total
        for i in range(1, 4):
            conn.execute("""
            INSERT INTO topics (subject_id, unit, name, difficulty, est_hours, weightage, mastery, status)
            VALUES (?, 'Unit 1', ?, 4, 2.0, ?, 0.1, 'not_started');
            """, (subj_id, f"Short Topic {i}", 1.0 + (i * 0.2)))

    res = create_schedule_internal(start_str, end_str)
    assert res["status"] == "success"
    # Zero unscheduled topics
    assert len(res["unscheduled_topics"]) == 0, f"Expected 0 unscheduled, got {res['unscheduled_topics']}"
    assert res["unscheduled_hours"] == 0.0

    # Every task for this subject must be type 'revision'
    with get_connection() as conn:
        study_tasks = conn.execute("""
        SELECT t.id, t.type FROM tasks t
        JOIN topics tp ON t.topic_id = tp.id
        WHERE tp.subject_id = ? AND t.type = 'study';
        """, (subj_id,)).fetchall()
        assert len(study_tasks) == 0, f"Expected no 'study' tasks for short-notice subject, found {len(study_tasks)}"

        # Total scheduled revision hours must cover the 6h needed
        total_row = conn.execute("""
        SELECT SUM(duration_hours) as total FROM tasks t
        JOIN topics tp ON t.topic_id = tp.id
        WHERE tp.subject_id = ? AND t.type = 'revision' AND t.status != 'missed';
        """, (subj_id,)).fetchone()
        total_rev_hrs = total_row["total"] or 0.0
        assert total_rev_hrs >= 6.0 - 0.05, f"Expected >= 6h revision, got {total_rev_hrs}"

    # Conflict audit: zero conflicts
    audit = check_conflicts_internal()
    assert audit["conflicts_found"] is False, f"Expected 0 conflicts, got {audit['conflicts']}"
    assert len(audit["conflicts"]) == 0, f"Conflicts: {audit['conflicts']}"


def test_normal_subject_unchanged_study_plus_revision():
    """
    A normal subject (exam far away) still uses 'study' tasks for learning
    and gets supplementary revision tasks (max 1h each) in the last 2 days.
    Normal revision tasks do NOT reduce unscheduled study hours.
    """
    today = date.today()
    start_str = today.isoformat()
    end_str = (today + timedelta(days=14)).isoformat()
    # Exam 10 days away — well outside short-notice window
    exam_str = (today + timedelta(days=10)).isoformat()

    with get_db() as conn:
        conn.execute("DELETE FROM tasks;")
        conn.execute("DELETE FROM topics;")
        conn.execute("DELETE FROM subjects;")

        conn.execute("UPDATE profile SET max_daily_hours = 6.0, break_minutes = 15 WHERE id = 1;")

        conn.execute("DELETE FROM free_slots;")
        for day_idx in range(7):
            conn.execute("""
            INSERT INTO free_slots (weekday, start_time, end_time, energy)
            VALUES (?, '09:00', '18:00', 'high');
            """, (day_idx,))

        cur = conn.execute("""
        INSERT INTO subjects (name, exam_date, priority, color)
        VALUES ('Normal Subject', ?, 1, '#3B82F6');
        """, (exam_str,))
        subj_id = cur.lastrowid

        # 3 topics, 2h each
        topic_ids = []
        for i in range(1, 4):
            c = conn.execute("""
            INSERT INTO topics (subject_id, unit, name, difficulty, est_hours, weightage, mastery, status)
            VALUES (?, 'Unit 1', ?, 3, 2.0, 1.5, 0.2, 'not_started');
            """, (subj_id, f"Normal Topic {i}"))
            topic_ids.append(c.lastrowid)

    res = create_schedule_internal(start_str, end_str)
    assert res["status"] == "success"

    with get_connection() as conn:
        # Must have study tasks
        study_count = conn.execute("""
        SELECT COUNT(*) as c FROM tasks t
        JOIN topics tp ON t.topic_id = tp.id
        WHERE tp.subject_id = ? AND t.type = 'study';
        """, (subj_id,)).fetchone()["c"]
        assert study_count > 0, "Normal subject should have study tasks"

        # Must have revision tasks in last 2 days before exam
        exam_d = date.fromisoformat(exam_str)
        buffer_start = (exam_d - timedelta(days=2)).isoformat()
        rev_count = conn.execute("""
        SELECT COUNT(*) as c FROM tasks t
        JOIN topics tp ON t.topic_id = tp.id
        WHERE tp.subject_id = ? AND t.type = 'revision'
        AND t.date >= ? AND t.date < ?;
        """, (subj_id, buffer_start, exam_str)).fetchone()["c"]
        assert rev_count > 0, "Normal subject should have revision tasks in 48h buffer"

    # No conflicts
    audit = check_conflicts_internal()
    buffer_viols = [c for c in audit["conflicts"] if c["type"] == "revision_buffer_violation"]
    assert len(buffer_viols) == 0, f"Unexpected revision_buffer_violation: {buffer_viols}"


def test_short_notice_not_displaced_by_normal_subjects():
    """
    Exam-in-2-days subject with 3 topics (2h each, 6h total) alongside two
    normal subjects. 4h/day slots 18:00-22:00. The short-notice subject must
    get every topic its full 2h and never be displaced by energy matching or
    subject interleaving with the normal subjects.
    """
    today = date.today()
    start_str = today.isoformat()
    end_str = (today + timedelta(days=7)).isoformat()
    short_exam = (today + timedelta(days=2)).isoformat()
    normal_exam_1 = (today + timedelta(days=14)).isoformat()
    normal_exam_2 = (today + timedelta(days=21)).isoformat()

    with get_db() as conn:
        conn.execute("DELETE FROM tasks;")
        conn.execute("DELETE FROM topics;")
        conn.execute("DELETE FROM subjects;")

        # 4h/day, slots 18:00-22:00
        conn.execute("UPDATE profile SET max_daily_hours = 4.0, break_minutes = 15 WHERE id = 1;")
        conn.execute("DELETE FROM free_slots;")
        for day_idx in range(7):
            conn.execute("""
            INSERT INTO free_slots (weekday, start_time, end_time, energy)
            VALUES (?, '18:00', '22:00', 'high');
            """, (day_idx,))

        # Short-notice subject: exam in 2 days, 3 topics * 2h = 6h
        cur = conn.execute("""
        INSERT INTO subjects (name, exam_date, priority, color)
        VALUES ('Urgent Subject', ?, 1, '#EF4444');
        """, (short_exam,))
        urgent_id = cur.lastrowid
        for i in range(1, 4):
            conn.execute("""
            INSERT INTO topics (subject_id, unit, name, difficulty, est_hours, weightage, mastery, status)
            VALUES (?, 'Unit 1', ?, 4, 2.0, 2.0, 0.1, 'not_started');
            """, (urgent_id, f"Urgent Topic {i}"))

        # Normal subject 1: exam in 14 days
        cur = conn.execute("""
        INSERT INTO subjects (name, exam_date, priority, color)
        VALUES ('Normal Subject A', ?, 2, '#3B82F6');
        """, (normal_exam_1,))
        norm1_id = cur.lastrowid
        for i in range(1, 4):
            conn.execute("""
            INSERT INTO topics (subject_id, unit, name, difficulty, est_hours, weightage, mastery, status)
            VALUES (?, 'Unit 1', ?, 5, 2.0, 1.5, 0.2, 'not_started');
            """, (norm1_id, f"Normal A Topic {i}"))

        # Normal subject 2: exam in 21 days
        cur = conn.execute("""
        INSERT INTO subjects (name, exam_date, priority, color)
        VALUES ('Normal Subject B', ?, 3, '#10B981');
        """, (normal_exam_2,))
        norm2_id = cur.lastrowid
        for i in range(1, 3):
            conn.execute("""
            INSERT INTO topics (subject_id, unit, name, difficulty, est_hours, weightage, mastery, status)
            VALUES (?, 'Unit 1', ?, 5, 2.0, 1.5, 0.3, 'not_started');
            """, (norm2_id, f"Normal B Topic {i}"))

    res = create_schedule_internal(start_str, end_str)
    assert res["status"] == "success"

    # Short-notice subject must have zero unscheduled topics
    urgent_unsched = [u for u in res["unscheduled_topics"] if u["subject_id"] == urgent_id]
    assert len(urgent_unsched) == 0, f"Short-notice topics left unscheduled: {urgent_unsched}"

    with get_connection() as conn:
        # All tasks for the urgent subject must be 'revision'
        urgent_study = conn.execute("""
        SELECT COUNT(*) as c FROM tasks t
        JOIN topics tp ON t.topic_id = tp.id
        WHERE tp.subject_id = ? AND t.type = 'study';
        """, (urgent_id,)).fetchone()["c"]
        assert urgent_study == 0, f"Expected no 'study' tasks for short-notice subject, found {urgent_study}"

        # Total scheduled hours for urgent subject must cover 6h
        total_row = conn.execute("""
        SELECT SUM(duration_hours) as total FROM tasks t
        JOIN topics tp ON t.topic_id = tp.id
        WHERE tp.subject_id = ? AND t.status != 'missed';
        """, (urgent_id,)).fetchone()
        total_hrs = total_row["total"] or 0.0
        assert total_hrs >= 6.0 - 0.05, f"Expected >= 6h for urgent subject, got {total_hrs}"

        # Each of the 3 urgent topics must have gotten its full 2h
        for i in range(1, 4):
            topic_row = conn.execute("""
            SELECT tp.id, SUM(t.duration_hours) as total
            FROM tasks t
            JOIN topics tp ON t.topic_id = tp.id
            WHERE tp.subject_id = ? AND tp.name = ?
            GROUP BY tp.id;
            """, (urgent_id, f"Urgent Topic {i}")).fetchone()
            assert topic_row is not None, f"Urgent Topic {i} has no tasks"
            assert topic_row["total"] >= 2.0 - 0.05, (
                f"Urgent Topic {i} got only {topic_row['total']}h, expected 2h"
            )

    # Conflict audit: zero conflicts for the short-notice subject
    audit = check_conflicts_internal()
    buffer_viols = [c for c in audit["conflicts"] if c["type"] == "revision_buffer_violation"]
    assert len(buffer_viols) == 0, f"Unexpected revision_buffer_violation: {buffer_viols}"
    unsched_conflict = [c for c in audit["conflicts"] if c["type"] == "unscheduled_topics"]
    if unsched_conflict:
        unsched_topic_ids = [t["subject_id"] for t in unsched_conflict[0].get("topics", []) if "subject_id" in t]
        assert urgent_id not in unsched_topic_ids, "Urgent subject must not appear in unscheduled_topics conflict"


@pytest.mark.anyio
async def test_compress_plan_after_new_plan_uses_dynamic_subject_id():
    """
    After reset + new setup that produces an unscheduled_topics conflict, the
    mock's compress_plan tool call must use the correct dynamic subject_id
    (not hard-coded 1) and must not return an execution_error.
    """
    import json as _json
    from app.agents.planner import PlannerAgent
    from app.db import set_current_llm_mode, reset_to_new_plan

    set_current_llm_mode("mock")

    # 1. Reset everything (simulates /api/plan/new)
    reset_to_new_plan()

    today = date.today()

    # 2. Setup with constrained capacity so unscheduled_topics conflict occurs
    with get_db() as conn:
        conn.execute("""
        INSERT OR REPLACE INTO profile (id, max_daily_hours, preferred_time, break_minutes, burnout_threshold_days)
        VALUES (1, 0.5, 'evening', 10, 3);
        """)
        conn.execute("DELETE FROM free_slots;")
        for d in range(7):
            conn.execute("""
            INSERT INTO free_slots (weekday, start_time, end_time, energy)
            VALUES (?, '18:00', '18:30', 'medium');
            """, (d,))

        cur = conn.execute("""
        INSERT INTO subjects (name, exam_date, priority, color)
        VALUES ('Post-Reset Subject', ?, 1, '#EF4444');
        """, ((today + timedelta(days=4)).isoformat(),))
        new_subj_id = cur.lastrowid

        for i in range(1, 6):
            conn.execute("""
            INSERT INTO topics (subject_id, unit, name, difficulty, est_hours, weightage, mastery, status)
            VALUES (?, 'U1', ?, 3, 3.0, 1.5, 0.0, 'not_started');
            """, (new_subj_id, f"Topic {i}"))

    # 3. Run the planner agent (mock mode) — it will hit unscheduled_topics and call compress_plan
    agent = PlannerAgent()
    result = await agent.run()

    assert result["status"] == "success"
    run_id = result["run_id"]

    # 4. Query agent_logs for compress_plan calls
    with get_connection() as conn:
        logs = conn.execute("""
        SELECT tool_name, args_json, result_json
        FROM agent_logs
        WHERE run_id = ? AND tool_name = 'compress_plan';
        """, (run_id,)).fetchall()

    # compress_plan should have been called (capacity is tiny -> unscheduled conflict)
    assert len(logs) >= 1, "Expected compress_plan to be called due to unscheduled_topics conflict"

    for log in logs:
        # Must not have execution_error
        result_data = _json.loads(log["result_json"])
        assert result_data.get("status") != "execution_error", (
            f"compress_plan returned execution_error: {result_data}"
        )
        # Must use the correct subject_id
        args_data = _json.loads(log["args_json"])
        assert args_data.get("subject_id") == new_subj_id, (
            f"compress_plan used subject_id={args_data.get('subject_id')}, expected {new_subj_id}"
        )

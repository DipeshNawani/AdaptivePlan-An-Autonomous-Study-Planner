"""FastAPI application with REST API endpoints, SSE live agent stream, and static UI mount."""
import json
import uuid
import asyncio
from pathlib import Path
from datetime import date, datetime
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response

from app.config import STATIC_DIR, GEMINI_MODEL, GEMINI_API_KEY, BASE_DIR
from app.db import (
    init_db, seed_demo_data, get_db, get_connection,
    get_current_llm_mode, set_current_llm_mode, get_is_demo, set_is_demo, reset_to_new_plan
)
from app.models import (
    ProfileModel, SetupRequest, TaskStatusUpdate, ReplanRequest, PanicRequest,
    QuizGenerateRequest, QuizSubmitRequest, LLMModeRequest,
    SubjectCreate, SubjectUpdate, TopicCreate, TopicUpdate, PastPaperWeightageRequest
)
from app.agents.planner import PlannerAgent
from app.agents.coach import CoachAgent
from app.llm.gemini import test_gemini_connection
from app.tools.syllabus import parse_syllabus_internal
from app.tools.progress import mark_progress_internal
from app.tools.scheduling import replan_internal, compress_plan_internal
from app.tools.quiz import generate_quiz_internal, record_quiz_result_internal
from app.services.readiness import get_all_readiness_metrics
from app.services.export import generate_ics_calendar, generate_pdf_schedule

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    init_db()
    # A fresh install starts completely EMPTY!
    # Demo data is loaded ONLY when user clicks "Load Demo Student".
    yield

app = FastAPI(
    title="AdaptivePlan: An Autonomous Study Planner",
    description="LLM-Powered Autonomous Study Planning Agent with Dynamic Rescheduling for Engineering Students",
    version="1.1.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------- Status & Mode Management (PART A) -----------------

@app.get("/api/status")
async def get_system_status():
    """Return backend status, LLM mode, live availability, is_demo flag, and statistics."""
    current_mode = get_current_llm_mode()
    live_available = bool(GEMINI_API_KEY)

    with get_connection() as conn:
        subj_count = conn.execute("SELECT COUNT(*) as c FROM subjects;").fetchone()["c"]
        tasks_count = conn.execute("SELECT COUNT(*) as c FROM tasks;").fetchone()["c"]
        topics_count = conn.execute("SELECT COUNT(*) as c FROM topics;").fetchone()["c"]

    return {
        "status": "online",
        "mock_mode": (current_mode == "mock"),
        "llm_mode": current_mode,
        "live_available": live_available,
        "model": GEMINI_MODEL if current_mode == "live" else "Mock LLM Provider",
        "is_demo": get_is_demo() and subj_count > 0,
        "subjects_count": subj_count,
        "topics_count": topics_count,
        "tasks_count": tasks_count
    }

@app.get("/api/llm/mode")
async def get_llm_mode():
    """Return current LLM mode, live availability, model name, and connection reason."""
    mode = get_current_llm_mode()
    live_available = bool(GEMINI_API_KEY)
    if not live_available:
        reason = "Add GEMINI_API_KEY to .env to enable Live mode"
    elif mode == "live":
        reason = f"Active: Connected to {GEMINI_MODEL}"
    else:
        reason = "Active: Mock mode (offline deterministic provider)"

    return {
        "mode": mode,
        "live_available": live_available,
        "model": GEMINI_MODEL,
        "reason": reason
    }

@app.post("/api/llm/mode")
async def set_llm_mode(payload: LLMModeRequest):
    """Switch LLM mode at runtime without server restart."""
    if payload.mode == "live" and not GEMINI_API_KEY:
        raise HTTPException(
            status_code=400,
            detail="Add GEMINI_API_KEY to .env to enable Live mode"
        )

    try:
        set_current_llm_mode(payload.mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "status": "success",
        "mode": payload.mode,
        "live_available": bool(GEMINI_API_KEY),
        "model": GEMINI_MODEL if payload.mode == "live" else "Mock LLM Provider"
    }

@app.post("/api/llm/test")
async def test_llm():
    """Make one tiny Gemini call to verify API credentials and connectivity."""
    return test_gemini_connection()

# ----------------- Demo Data vs Real Data (PART B) -----------------

@app.post("/api/seed")
async def seed_data(force: bool = True):
    """Load demo student data with relative future exam dates and set is_demo=True."""
    res = seed_demo_data(force=force)
    return res

@app.post("/api/plan/new")
async def start_new_plan():
    """Clear all subjects, topics, tasks, quiz results, and agent logs to start fresh."""
    reset_to_new_plan()
    return {
        "status": "success",
        "message": "All current plan data cleared. You can now build a new plan."
    }

@app.get("/api/backup")
async def download_backup():
    """Download full JSON backup of current subjects, topics, and schedule."""
    with get_connection() as conn:
        profile = dict(conn.execute("SELECT * FROM profile WHERE id = 1;").fetchone() or {})
        free_slots = [dict(r) for r in conn.execute("SELECT * FROM free_slots ORDER BY weekday, start_time;").fetchall()]
        subjects_rows = conn.execute("SELECT * FROM subjects ORDER BY exam_date;").fetchall()
        subjects = []
        for s in subjects_rows:
            s_dict = dict(s)
            s_dict["topics"] = [dict(t) for t in conn.execute("SELECT * FROM topics WHERE subject_id = ?;", (s["id"],)).fetchall()]
            subjects.append(s_dict)
        tasks = [dict(t) for t in conn.execute("SELECT * FROM tasks;").fetchall()]

    backup = {
        "exported_at": datetime.now().isoformat(),
        "is_demo": get_is_demo(),
        "profile": profile,
        "free_slots": free_slots,
        "subjects": subjects,
        "tasks": tasks
    }
    filename = f"AdaptivePlan_Backup_{date.today().isoformat()}.json"
    return Response(
        content=json.dumps(backup, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# ----------------- Setup Wizard & Subject/Topic CRUD -----------------

@app.get("/api/setup/data")
async def get_setup_data():
    """Retrieve existing profile, subjects, and free slots for setup wizard."""
    with get_connection() as conn:
        profile = dict(conn.execute("SELECT * FROM profile WHERE id = 1;").fetchone() or {})
        free_slots = [dict(r) for r in conn.execute("SELECT * FROM free_slots ORDER BY weekday, start_time;").fetchall()]
        subjects_rows = conn.execute("SELECT * FROM subjects ORDER BY exam_date;").fetchall()
        subjects = []
        for s in subjects_rows:
            subj_dict = dict(s)
            topics = [dict(t) for t in conn.execute("SELECT * FROM topics WHERE subject_id = ?;", (s["id"],)).fetchall()]
            subj_dict["topics"] = topics
            subjects.append(subj_dict)

    return {
        "is_demo": get_is_demo() and len(subjects) > 0,
        "profile": profile,
        "free_slots": free_slots,
        "subjects": subjects
    }

@app.post("/api/profile")
async def save_profile(profile: ProfileModel):
    """Save student profile preferences."""
    with get_db() as conn:
        conn.execute("""
        INSERT OR REPLACE INTO profile (id, max_daily_hours, preferred_time, break_minutes, burnout_threshold_days)
        VALUES (1, ?, ?, ?, ?);
        """, (profile.max_daily_hours, profile.preferred_time, profile.break_minutes, profile.burnout_threshold_days))
    return {"status": "success", "message": "Profile updated"}

@app.post("/api/setup")
async def save_setup(payload: SetupRequest):
    """Save student profile, free study slots, and enrolled subjects (from New Plan wizard)."""
    with get_db() as conn:
        # Save profile
        conn.execute("""
        INSERT OR REPLACE INTO profile (id, max_daily_hours, preferred_time, break_minutes, burnout_threshold_days)
        VALUES (1, ?, ?, ?, ?);
        """, (
            payload.profile.max_daily_hours,
            payload.profile.preferred_time,
            payload.profile.break_minutes,
            payload.profile.burnout_threshold_days
        ))

        # Save free slots
        if payload.free_slots:
            conn.execute("DELETE FROM free_slots;")
            for slot in payload.free_slots:
                conn.execute("""
                INSERT INTO free_slots (weekday, start_time, end_time, energy)
                VALUES (?, ?, ?, ?);
                """, (slot.weekday, slot.start_time, slot.end_time, slot.energy))

        # Save subjects and topics
        if payload.subjects:
            conn.execute("DELETE FROM tasks;")
            conn.execute("DELETE FROM topics;")
            conn.execute("DELETE FROM subjects;")

            for subj in payload.subjects:
                cur = conn.execute("""
                INSERT INTO subjects (name, exam_date, priority, color)
                VALUES (?, ?, ?, ?);
                """, (subj.name, subj.exam_date, subj.priority, subj.color))
                s_id = cur.lastrowid

                for top in subj.topics:
                    conn.execute("""
                    INSERT INTO topics (subject_id, unit, name, difficulty, est_hours, weightage, mastery, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """, (s_id, top.unit, top.name, top.difficulty, top.est_hours, top.weightage, top.mastery, top.status))

    set_is_demo(False)
    return {"status": "success", "message": "Setup saved successfully"}

@app.get("/api/subjects")
async def get_subjects():
    """Get list of all enrolled subjects with their topics."""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM subjects ORDER BY exam_date;").fetchall()
        subjects = []
        for s in rows:
            s_dict = dict(s)
            s_dict["topics"] = [dict(t) for t in conn.execute("SELECT * FROM topics WHERE subject_id = ?;", (s["id"],)).fetchall()]
            subjects.append(s_dict)
    return subjects

@app.post("/api/subjects")
async def create_subject(subj: SubjectCreate):
    """Add a new subject to the study plan."""
    with get_db() as conn:
        cur = conn.execute("""
        INSERT INTO subjects (name, exam_date, priority, color)
        VALUES (?, ?, ?, ?);
        """, (subj.name, subj.exam_date, subj.priority, subj.color))
        s_id = cur.lastrowid

        for top in subj.topics:
            conn.execute("""
            INSERT INTO topics (subject_id, unit, name, difficulty, est_hours, weightage, mastery, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, (s_id, top.unit, top.name, top.difficulty, top.est_hours, top.weightage, top.mastery, top.status))

    set_is_demo(False)
    return {"status": "success", "subject_id": s_id, "message": f"Subject '{subj.name}' created"}

@app.put("/api/subjects/{subject_id}")
async def update_subject(subject_id: int, payload: SubjectUpdate):
    """Update subject properties. Changing an exam date automatically triggers a replan."""
    with get_connection() as conn:
        existing = conn.execute("SELECT * FROM subjects WHERE id = ?;", (subject_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Subject not found")

    new_name = payload.name or existing["name"]
    new_exam = payload.exam_date or existing["exam_date"]
    new_prio = payload.priority if payload.priority is not None else existing["priority"]
    new_color = payload.color or existing["color"]

    exam_date_changed = (new_exam != existing["exam_date"])

    with get_db() as conn:
        conn.execute("""
        UPDATE subjects SET name = ?, exam_date = ?, priority = ?, color = ? WHERE id = ?;
        """, (new_name, new_exam, new_prio, new_color, subject_id))

    replan_result = None
    if exam_date_changed:
        # Check if tasks exist
        with get_connection() as conn:
            task_count = conn.execute("SELECT COUNT(*) as c FROM tasks;").fetchone()["c"]
        if task_count > 0:
            replan_result = replan_internal(
                from_date=date.today().isoformat(),
                reason=f"Exam date updated for {new_name} to {new_exam}"
            )

    return {
        "status": "success",
        "subject_id": subject_id,
        "exam_date_changed": exam_date_changed,
        "replan_details": replan_result
    }

@app.delete("/api/subjects/{subject_id}")
async def delete_subject(subject_id: int):
    """Delete a subject and cascade delete its topics and scheduled tasks."""
    with get_db() as conn:
        conn.execute("DELETE FROM subjects WHERE id = ?;", (subject_id,))

    # Trigger replan if tasks remain
    with get_connection() as conn:
        task_count = conn.execute("SELECT COUNT(*) as c FROM tasks;").fetchone()["c"]

    replan_result = None
    if task_count > 0:
        replan_result = replan_internal(
            from_date=date.today().isoformat(),
            reason=f"Subject #{subject_id} was removed"
        )

    return {"status": "success", "message": "Subject deleted", "replan_details": replan_result}

@app.post("/api/topics")
async def create_topic(topic: TopicCreate, subject_id: Optional[int] = None):
    """Add a topic to a subject."""
    target_subject_id = subject_id or topic.subject_id
    if not target_subject_id:
        raise HTTPException(status_code=400, detail="subject_id is required either in path/query or topic payload")

    with get_db() as conn:
        cur = conn.execute("""
        INSERT INTO topics (subject_id, unit, name, difficulty, est_hours, weightage, mastery, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """, (target_subject_id, topic.unit, topic.name, topic.difficulty, topic.est_hours, topic.weightage, topic.mastery, topic.status))
        t_id = cur.lastrowid
        row = conn.execute("SELECT * FROM topics WHERE id = ?;", (t_id,)).fetchone()

    return {"status": "success", "topic_id": t_id, "topic": dict(row) if row else {}}

@app.put("/api/topics/{topic_id}")
async def update_topic(topic_id: int, payload: TopicUpdate):
    """Update topic difficulty, estimated hours, or name."""
    with get_connection() as conn:
        existing = conn.execute("SELECT * FROM topics WHERE id = ?;", (topic_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Topic not found")

    new_unit = payload.unit or existing["unit"]
    new_name = payload.name or existing["name"]
    new_diff = payload.difficulty if payload.difficulty is not None else existing["difficulty"]
    new_hours = payload.est_hours if payload.est_hours is not None else existing["est_hours"]
    new_weight = payload.weightage if payload.weightage is not None else existing["weightage"]
    new_mastery = payload.mastery if payload.mastery is not None else existing["mastery"]
    new_status = payload.status or existing["status"]

    with get_db() as conn:
        conn.execute("""
        UPDATE topics SET unit = ?, name = ?, difficulty = ?, est_hours = ?, weightage = ?, mastery = ?, status = ?
        WHERE id = ?;
        """, (new_unit, new_name, new_diff, new_hours, new_weight, new_mastery, new_status, topic_id))
        updated = conn.execute("SELECT * FROM topics WHERE id = ?;", (topic_id,)).fetchone()

    return {"status": "success", "topic_id": topic_id, "topic": dict(updated) if updated else {}}


@app.delete("/api/topics/{topic_id}")
async def delete_topic(topic_id: int):
    """Delete a topic and its associated tasks."""
    with get_db() as conn:
        conn.execute("DELETE FROM tasks WHERE topic_id = ?;", (topic_id,))
        conn.execute("DELETE FROM topics WHERE id = ?;", (topic_id,))

    return {"status": "success", "topic_id": topic_id}

@app.post("/api/syllabus/weightage")
async def analyze_past_paper_weightage(payload: PastPaperWeightageRequest):
    """
    Step 4: Past-paper topics for weightage.
    Analyzes past question paper text, counts frequency of matching topic keywords,
    and boosts topic weightage accordingly.
    """
    text_lower = payload.text.lower()
    updated_topics = []

    with get_db() as conn:
        topics = conn.execute("SELECT id, name, weightage FROM topics;").fetchall()
        for t in topics:
            keywords = [w for w in t["name"].lower().split() if len(w) > 3]
            match_count = sum(text_lower.count(k) for k in keywords)
            if match_count > 0:
                boost = min(1.5, match_count * 0.2)
                new_weight = round(t["weightage"] + boost, 2)
                conn.execute("UPDATE topics SET weightage = ? WHERE id = ?;", (new_weight, t["id"]))
                updated_topics.append({"id": t["id"], "name": t["name"], "new_weightage": new_weight})

    return {
        "status": "success",
        "matched_count": len(updated_topics),
        "updated_topics": updated_topics
    }

@app.post("/api/syllabus")
async def parse_syllabus(
    text: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    subject_id: Optional[int] = Form(None),
    subject_name: Optional[str] = Form(None),
    save_to_db: bool = Form(True)
):
    """Parse syllabus from raw pasted text or uploaded PDF file."""
    temp_path = None
    try:
        if file and file.filename:
            file_bytes = await file.read()
            clean_name = Path(file.filename).name
            temp_file = BASE_DIR / "data" / f"temp_{uuid.uuid4()}_{clean_name}"
            temp_path = str(temp_file)
            with open(temp_path, "wb") as f:
                f.write(file_bytes)
            res = parse_syllabus_internal(file_id=temp_path, subject_id=subject_id, subject_name=subject_name, save_to_db=save_to_db)
        elif text:
            res = parse_syllabus_internal(text=text, subject_id=subject_id, subject_name=subject_name, save_to_db=save_to_db)
        else:
            raise HTTPException(status_code=400, detail="Either syllabus text or PDF file must be provided")

        return res
    finally:
        if temp_path:
            import os
            try:
                os.remove(temp_path)
            except Exception:
                pass

@app.post("/api/syllabus/bulk")
async def parse_bulk_syllabus(
    text: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None)
):
    """
    Parse a full curriculum/syllabus document containing multiple subjects and topics (.txt or .pdf).
    Auto-detects subject headings, units, topics, and assigns future exam dates and colors.
    Returns structured subjects and topics for user review.
    """
    from app.tools.syllabus import parse_multi_subject_syllabus
    raw_text = ""
    temp_path = None
    try:
        if file and file.filename:
            file_bytes = await file.read()
            clean_name = Path(file.filename).name
            if clean_name.lower().endswith(".pdf"):
                temp_file = BASE_DIR / "data" / f"temp_{uuid.uuid4()}_{clean_name}"
                temp_path = str(temp_file)
                with open(temp_path, "wb") as f:
                    f.write(file_bytes)
                from pypdf import PdfReader
                reader = PdfReader(temp_path)
                for page in reader.pages:
                    raw_text += (page.extract_text() or "") + "\n"
            else:
                try:
                    raw_text = file_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    raw_text = file_bytes.decode("latin-1", errors="ignore")
        elif text:
            raw_text = text
        else:
            raise HTTPException(status_code=400, detail="Please upload a syllabus file (.txt / .pdf) or paste syllabus text.")

        if not raw_text.strip():
            raise HTTPException(status_code=400, detail="The provided file or text is empty.")

        subjects = parse_multi_subject_syllabus(raw_text)
        if not subjects:
            raise HTTPException(status_code=400, detail="Could not identify subjects or topics in the provided text.")

        total_topics = sum(len(s.get("topics", [])) for s in subjects)
        return {
            "status": "success",
            "message": f"Successfully detected {len(subjects)} subjects and {total_topics} topics.",
            "subjects_count": len(subjects),
            "total_topics": total_topics,
            "subjects": subjects
        }
    finally:
        if temp_path:
            import os
            try:
                os.remove(temp_path)
            except Exception:
                pass

@app.get("/api/syllabus/export-json")
async def export_syllabus_json():
    """Export all enrolled subjects and topics as portable JSON."""
    with get_connection() as conn:
        subjects_rows = conn.execute("SELECT * FROM subjects ORDER BY exam_date;").fetchall()
        subjects = []
        for s in subjects_rows:
            subj_dict = dict(s)
            topics = [dict(t) for t in conn.execute("SELECT * FROM topics WHERE subject_id = ?;", (s["id"],)).fetchall()]
            subj_dict["topics"] = topics
            subjects.append(subj_dict)
    return {"subjects": subjects}

# ----------------- Plan Generation & Live Agent Streaming -----------------

@app.post("/api/plan/generate")
async def generate_plan(background_tasks: BackgroundTasks):
    """
    Launch the autonomous Planner Agent.
    Runs inside a background task, streaming live tool calls to SSE.
    """
    with get_connection() as conn:
        subj_count = conn.execute("SELECT COUNT(*) as c FROM subjects;").fetchone()["c"]
        topics_count = conn.execute("SELECT COUNT(*) as c FROM topics;").fetchone()["c"]

    if subj_count == 0 or topics_count == 0:
        raise HTTPException(status_code=400, detail="Cannot generate plan with zero enrolled subjects or topics. Please create a plan first.")

    run_id = str(uuid.uuid4())
    agent = PlannerAgent(run_id=run_id)

    # Spawn planner in asynchronous background task
    asyncio.create_task(agent.run())

    return {
        "status": "started",
        "run_id": run_id,
        "message": "Planner Agent started. Connect to SSE stream for live activity."
    }

@app.get("/api/agent/stream/{run_id}")
async def stream_agent_activity(run_id: str):
    """Server-Sent Events (SSE) live stream of agent thoughts and tool calls."""
    from app.agents.planner import get_or_create_queue, AGENT_RUN_HISTORY

    queue = get_or_create_queue(run_id)

    async def event_generator():
        # First send any historical events already logged for this run
        history = list(AGENT_RUN_HISTORY.get(run_id, []))
        sent_count = 0
        is_already_done = False
        for event in history:
            yield f"data: {json.dumps(event)}\n\n"
            sent_count += 1
            if event.get("type") in ("done", "error"):
                is_already_done = True
                break

        if is_already_done:
            return

        # Drain from queue whatever was already emitted via history
        for _ in range(sent_count):
            if not queue.empty():
                try:
                    queue.get_nowait()
                except Exception:
                    pass

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
            except Exception:
                break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )

@app.get("/api/agent/logs/{run_id}")
async def get_agent_logs(run_id: str):
    """Retrieve stored tool logs for a given run from SQLite."""
    with get_connection() as conn:
        rows = conn.execute("""
        SELECT id, run_id, step, tool_name, args_json, result_json, thought, created_at
        FROM agent_logs
        WHERE run_id = ?
        ORDER BY step;
        """, (run_id,)).fetchall()

    return {"run_id": run_id, "logs": [dict(r) for r in rows]}

@app.get("/api/agent/runs")
async def get_agent_runs():
    """List all past agent runs with metadata, newest first."""
    with get_connection() as conn:
        rows = conn.execute("""
        SELECT id, provider, mode, status, iterations, summary, is_fallback, started_at, finished_at
        FROM agent_runs
        ORDER BY started_at DESC
        LIMIT 50;
        """).fetchall()
    return {"runs": [dict(r) for r in rows]}

@app.get("/api/agent/runs/{run_id}/events")
async def get_agent_run_events(run_id: str):
    """Retrieve full event replay for a past run from SQLite (survives restart)."""
    from app.agents.planner import AGENT_RUN_HISTORY
    # Try in-memory first (current session)
    if run_id in AGENT_RUN_HISTORY:
        return {"run_id": run_id, "events": AGENT_RUN_HISTORY[run_id]}
    # Fall back to persisted DB
    with get_connection() as conn:
        row = conn.execute("SELECT events_json FROM agent_runs WHERE id = ?;", (run_id,)).fetchone()
    if not row or not row["events_json"]:
        return {"run_id": run_id, "events": []}
    return {"run_id": run_id, "events": json.loads(row["events_json"])}

# ----------------- Plan & Tasks -----------------

@app.get("/api/plan")
async def get_plan():
    """Retrieve all study tasks joined with subject, unit, and color information."""
    with get_connection() as conn:
        tasks = conn.execute("""
        SELECT t.id, t.topic_id, t.type, t.date, t.start_time, t.end_time, t.duration_hours,
               t.status, t.reason, t.priority_score,
               tp.name as topic_name, tp.unit, tp.difficulty, tp.weightage, tp.mastery,
               s.id as subject_id, s.name as subject_name, s.color, s.exam_date
        FROM tasks t
        JOIN topics tp ON t.topic_id = tp.id
        JOIN subjects s ON tp.subject_id = s.id
        ORDER BY t.date, t.start_time;
        """).fetchall()

    return {"tasks": [dict(t) for t in tasks]}

@app.post("/api/tasks/{task_id}/status")
async def update_task_status(task_id: int, payload: TaskStatusUpdate):
    """Update progress on a study task."""
    try:
        res = mark_progress_internal(task_id=task_id, status=payload.status)
        return res
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

@app.post("/api/plan/replan")
async def manual_replan(payload: ReplanRequest):
    """Manual trigger to redistribute unfinished/missed tasks from anchor date."""
    anchor_date = payload.from_date or date.today().isoformat()
    res = replan_internal(from_date=anchor_date, reason=payload.reason)
    return res

@app.post("/api/plan/panic")
async def panic_mode(payload: PanicRequest):
    """Activate Panic Mode: prune low-yield topics and compress schedule for an exam."""
    try:
        res = compress_plan_internal(subject_id=payload.subject_id, days_left=payload.days_left)
        return res
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

@app.get("/api/tasks/{task_id}/explain")
async def explain_task(task_id: int):
    """Provide mathematical and pedagogical explanation for why this task was scheduled on this date."""
    with get_connection() as conn:
        task = conn.execute("""
        SELECT t.id, t.date, t.start_time, t.end_time, t.duration_hours, t.type, t.reason, t.priority_score,
               tp.id as topic_id, tp.name as topic_name, tp.unit, tp.difficulty, tp.weightage, tp.mastery,
               s.name as subject_name, s.exam_date
        FROM tasks t
        JOIN topics tp ON t.topic_id = tp.id
        JOIN subjects s ON tp.subject_id = s.id
        WHERE t.id = ?;
        """, (task_id,)).fetchone()

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        w = date.fromisoformat(task["date"]).weekday()
        slot = conn.execute("""
        SELECT energy FROM free_slots WHERE weekday = ? AND start_time <= ? AND end_time >= ? LIMIT 1;
        """, (w, task["start_time"], task["end_time"])).fetchone()
        energy = slot["energy"] if slot else "standard"

    days_to_exam = (date.fromisoformat(task["exam_date"]) - date.fromisoformat(task["date"])).days
    mastery_deficit = round(1.0 + max(0.0, 1.0 - (task["mastery"] or 0.0)), 2)

    return {
        "task_id": task["id"],
        "topic_name": task["topic_name"],
        "subject_name": task["subject_name"],
        "scheduled_date": task["date"],
        "time_window": f"{task['start_time']} - {task['end_time']} ({task['duration_hours']}h)",
        "priority_score": task["priority_score"],
        "priority_factors": {
            "days_until_exam": days_to_exam,
            "difficulty_rating": task["difficulty"],
            "exam_weightage": task["weightage"],
            "current_mastery": f"{int((task['mastery'] or 0.0)*100)}%",
            "mastery_deficit_multiplier": mastery_deficit,
            "energy_window": f"{energy}-energy slot"
        },
        "reasoning": (
            f"Scheduled on {task['date']} because {task['subject_name']} exam is in {days_to_exam} days. "
            f"Topic difficulty is {task['difficulty']}/5 (placed in {energy} slot) with paper weightage {task['weightage']}. "
            f"Pre-exam revision buffer reserved strictly within 48h of {task['exam_date']}."
        )
    }

# ----------------- Dashboard & Coach -----------------

@app.get("/api/dashboard")
async def get_dashboard_data():
    """Retrieve progress metrics, readiness gauge values, and task distributions."""
    readiness_list = get_all_readiness_metrics()

    with get_connection() as conn:
        total_tasks = conn.execute("SELECT COUNT(*) as c FROM tasks;").fetchone()["c"]
        done_tasks = conn.execute("SELECT COUNT(*) as c FROM tasks WHERE status = 'done';").fetchone()["c"]
        missed_tasks = conn.execute("SELECT COUNT(*) as c FROM tasks WHERE status = 'missed';").fetchone()["c"]
        pending_tasks = conn.execute("SELECT COUNT(*) as c FROM tasks WHERE status = 'pending';").fetchone()["c"]

        types_rows = conn.execute("SELECT type, COUNT(*) as count FROM tasks GROUP BY type;").fetchall()
        types_breakdown = {r["type"]: r["count"] for r in types_rows}

        daily_hours = [dict(r) for r in conn.execute("""
        SELECT date, SUM(duration_hours) as hours, COUNT(*) as tasks
        FROM tasks
        GROUP BY date
        ORDER BY date
        LIMIT 14;
        """).fetchall()]

    from app.tools.scheduling import check_conflicts_internal
    conflict_data = check_conflicts_internal()

    return {
        "readiness": readiness_list,
        "summary": {
            "total_tasks": total_tasks,
            "done_tasks": done_tasks,
            "missed_tasks": missed_tasks,
            "pending_tasks": pending_tasks,
            "completion_percentage": round((done_tasks / max(1, total_tasks)) * 100, 1) if total_tasks > 0 else 0.0
        },
        "task_types": types_breakdown,
        "daily_hours": daily_hours,
        "conflicts": conflict_data["conflicts"]
    }

@app.get("/api/coach")
async def get_coach_feedback():
    """Invoke the Coach Agent to review study plan and check for burnout."""
    coach = CoachAgent()
    return coach.review_plan()

@app.get("/api/checkin")
async def get_daily_checkin():
    """Motivational and practical daily check-in message for today."""
    today_str = date.today().isoformat()
    with get_connection() as conn:
        today_tasks = conn.execute("""
        SELECT t.id, t.start_time, t.duration_hours, tp.name as topic_name, tp.difficulty, s.name as subject_name
        FROM tasks t
        JOIN topics tp ON t.topic_id = tp.id
        JOIN subjects s ON tp.subject_id = s.id
        WHERE t.date = ?
        ORDER BY t.start_time;
        """, (today_str,)).fetchall()

    if not today_tasks:
        return {
            "date": today_str,
            "task_count": 0,
            "message": "No study tasks scheduled for today! Enjoy your rest day or do light active recall."
        }

    hardest = max(today_tasks, key=lambda x: x["difficulty"])
    first = today_tasks[0]

    msg = (
        f"You have {len(today_tasks)} study task(s) today. Your hardest session is '{hardest['topic_name']}' "
        f"(Difficulty {hardest['difficulty']}/5). Start strong with your first session at {first['start_time']}!"
    )
    return {
        "date": today_str,
        "task_count": len(today_tasks),
        "hardest_topic": hardest["topic_name"],
        "first_start": first["start_time"],
        "message": msg
    }

# ----------------- Quiz Flow -----------------

@app.post("/api/quiz/generate")
async def generate_quiz(payload: QuizGenerateRequest):
    """Generate MCQ test for a topic."""
    try:
        res = generate_quiz_internal(topic_id=payload.topic_id, n=payload.n)
        return res
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))

@app.post("/api/quiz/submit")
async def submit_quiz(payload: QuizSubmitRequest):
    """Grade quiz answers, record result, update mastery, and trigger weak-topic remedial loop if needed."""
    total = len(payload.questions)
    if total == 0:
        raise HTTPException(status_code=400, detail="Quiz contains no questions")

    from app.tools.quiz import check_quiz_answer, resolve_correct_answer_display
    correct = 0
    detailed_results = []
    for q, user_ans in zip(payload.questions, payload.answers):
        is_correct = check_quiz_answer(q.options, q.answer, user_ans)
        display_correct = resolve_correct_answer_display(q.options, q.answer)
        if is_correct:
            correct += 1
        detailed_results.append({
            "question": q.question,
            "user_answer": user_ans,
            "correct_answer": display_correct,
            "is_correct": is_correct,
            "explanation": q.explanation
        })

    score = round((correct / total) * 100.0, 1)
    res = record_quiz_result_internal(topic_id=payload.topic_id, score=score, total=total, correct=correct)
    res["score"] = score
    res["correct_count"] = correct
    res["total_count"] = total
    res["question_results"] = detailed_results
    return res

# ----------------- Exports -----------------

@app.get("/api/export/ics")
async def export_calendar_ics():
    """Download study schedule as an iCalendar (.ics) file."""
    ics_content = generate_ics_calendar()
    return Response(
        content=ics_content,
        media_type="text/calendar",
        headers={"Content-Disposition": "attachment; filename=AdaptivePlan_Schedule.ics"}
    )

@app.get("/api/export/pdf")
async def export_schedule_pdf():
    """Download study schedule as a printable PDF timetable."""
    pdf_bytes = generate_pdf_schedule()
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=AdaptivePlan_Schedule.pdf"}
    )

# ----------------- Static UI Mount -----------------

STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

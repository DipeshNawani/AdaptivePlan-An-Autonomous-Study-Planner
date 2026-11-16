"""Tool registry exposing functions to the LLM agent with strict validation and schemas."""
import logging
from typing import Dict, Any, List
from pydantic import ValidationError

from app.models import (
    ParseSyllabusToolArgs, EstimateDifficultyToolArgs, GetFreeSlotsToolArgs,
    CreateScheduleToolArgs, CheckConflictsToolArgs, AddRevisionSessionsToolArgs,
    MarkProgressToolArgs, ReplanToolArgs, CompressPlanToolArgs,
    GenerateQuizToolArgs, RecordQuizResultToolArgs, SendReminderToolArgs
)
from app.tools.scheduling import (
    get_free_slots_internal, create_schedule_internal, check_conflicts_internal,
    replan_internal, compress_plan_internal
)
from app.tools.syllabus import parse_syllabus_internal
from app.tools.progress import mark_progress_internal, add_revision_sessions_internal
from app.tools.quiz import generate_quiz_internal, record_quiz_result_internal
from app.tools.reminders import send_reminder_internal
from app.db import get_connection

logger = logging.getLogger(__name__)

def estimate_difficulty_internal(topic_id: int) -> Dict[str, Any]:
    """Estimate topic difficulty and required study hours."""
    with get_connection() as conn:
        topic = conn.execute("SELECT id, name, difficulty, est_hours, weightage FROM topics WHERE id = ?;", (topic_id,)).fetchone()
        if not topic:
            raise ValueError(f"Topic #{topic_id} not found")

    return {
        "status": "success",
        "topic_id": topic_id,
        "name": topic["name"],
        "difficulty": topic["difficulty"],
        "est_hours": topic["est_hours"],
        "weightage": topic["weightage"]
    }

# Mapping of tool name -> (callable, Pydantic argument validator class)
TOOL_DEFINITIONS = {
    "parse_syllabus": (
        parse_syllabus_internal,
        ParseSyllabusToolArgs,
        "Extracts subjects, units and topics from text or uploaded PDF.",
        {
            "type": "OBJECT",
            "properties": {
                "text": {"type": "STRING", "description": "Raw syllabus text"},
                "file_id": {"type": "STRING", "description": "Path to PDF syllabus file"}
            }
        }
    ),
    "estimate_difficulty": (
        estimate_difficulty_internal,
        EstimateDifficultyToolArgs,
        "Returns difficulty 1-5 and estimated study hours for a topic.",
        {
            "type": "OBJECT",
            "properties": {
                "topic_id": {"type": "INTEGER", "description": "Unique topic ID"}
            },
            "required": ["topic_id"]
        }
    ),
    "get_free_slots": (
        get_free_slots_internal,
        GetFreeSlotsToolArgs,
        "Returns student's available study hours and energy windows between start_date and end_date.",
        {
            "type": "OBJECT",
            "properties": {
                "start_date": {"type": "STRING", "description": "Start date in YYYY-MM-DD format"},
                "end_date": {"type": "STRING", "description": "End date in YYYY-MM-DD format"}
            },
            "required": ["start_date", "end_date"]
        }
    ),
    "create_schedule": (
        create_schedule_internal,
        CreateScheduleToolArgs,
        "Deterministic allocator: assigns tasks to days and slots by priority.",
        {
            "type": "OBJECT",
            "properties": {
                "start_date": {"type": "STRING", "description": "Start date in YYYY-MM-DD"},
                "end_date": {"type": "STRING", "description": "End date in YYYY-MM-DD"},
                "task_ids": {"type": "ARRAY", "items": {"type": "INTEGER"}, "description": "Optional list of task IDs"}
            },
            "required": ["start_date", "end_date"]
        }
    ),
    "check_conflicts": (
        check_conflicts_internal,
        CheckConflictsToolArgs,
        "Audits the schedule for daily hour overloads, time overlaps, and missing final revisions.",
        {
            "type": "OBJECT",
            "properties": {}
        }
    ),
    "add_revision_sessions": (
        add_revision_sessions_internal,
        AddRevisionSessionsToolArgs,
        "Adds spaced-repetition revisions or pre-exam buffer revisions before exam.",
        {
            "type": "OBJECT",
            "properties": {
                "task_id": {"type": "INTEGER", "description": "ID of completed task (optional)"},
                "subject_id": {"type": "INTEGER", "description": "ID of subject to add revision for (optional)"}
            }
        }
    ),
    "mark_progress": (
        mark_progress_internal,
        MarkProgressToolArgs,
        "Updates task status ('done', 'missed', 'needs_revision') and triggers automatic replan if missed.",
        {
            "type": "OBJECT",
            "properties": {
                "task_id": {"type": "INTEGER", "description": "Task ID"},
                "status": {"type": "STRING", "enum": ["done", "missed", "needs_revision"]}
            },
            "required": ["task_id", "status"]
        }
    ),
    "replan": (
        replan_internal,
        ReplanToolArgs,
        "Redistributes unfinished and missed tasks across remaining days while preserving completed tasks.",
        {
            "type": "OBJECT",
            "properties": {
                "from_date": {"type": "STRING", "description": "Replan anchor date YYYY-MM-DD"},
                "reason": {"type": "STRING", "description": "Explanation for replan"}
            },
            "required": ["from_date", "reason"]
        }
    ),
    "compress_plan": (
        compress_plan_internal,
        CompressPlanToolArgs,
        "Panic mode: prunes low-yield topics and keeps high-weightage topics when exam is critically near.",
        {
            "type": "OBJECT",
            "properties": {
                "subject_id": {"type": "INTEGER", "description": "Subject ID"},
                "days_left": {"type": "INTEGER", "description": "Days remaining until exam"}
            },
            "required": ["subject_id", "days_left"]
        }
    ),
    "generate_quiz": (
        generate_quiz_internal,
        GenerateQuizToolArgs,
        "Generates n multiple-choice questions with options, answer and explanation.",
        {
            "type": "OBJECT",
            "properties": {
                "topic_id": {"type": "INTEGER", "description": "Topic ID"},
                "n": {"type": "INTEGER", "description": "Number of questions (1-10)"}
            },
            "required": ["topic_id"]
        }
    ),
    "record_quiz_result": (
        record_quiz_result_internal,
        RecordQuizResultToolArgs,
        "Records quiz score, updates topic mastery, and triggers weak-topic revision loop if score < 60%.",
        {
            "type": "OBJECT",
            "properties": {
                "topic_id": {"type": "INTEGER", "description": "Topic ID"},
                "score": {"type": "NUMBER", "description": "Score between 0 and 100"}
            },
            "required": ["topic_id", "score"]
        }
    ),
    "send_reminder": (
        send_reminder_internal,
        SendReminderToolArgs,
        "Stores a reminder for the student in the database.",
        {
            "type": "OBJECT",
            "properties": {
                "message": {"type": "STRING", "description": "Reminder text"},
                "time": {"type": "STRING", "description": "Time string"}
            },
            "required": ["message", "time"]
        }
    )
}

def get_tools_schema() -> List[Dict[str, Any]]:
    """Return JSON schemas for all available tools."""
    schemas = []
    for name, (_, _, description, params) in TOOL_DEFINITIONS.items():
        schemas.append({
            "name": name,
            "description": description,
            "parameters": params
        })
    return schemas

def get_planner_tools_schema() -> List[Dict[str, Any]]:
    """
    Return JSON schemas for scheduling tools used by Planner Agent.
    Strictly excludes syllabus parsing tools so the LLM cannot hallucinate/inject fake subjects.
    """
    allowed = {
        "get_free_slots", "estimate_difficulty", "create_schedule",
        "add_revision_sessions", "check_conflicts", "replan", "compress_plan"
    }
    schemas = []
    for name, (_, _, description, params) in TOOL_DEFINITIONS.items():
        if name in allowed:
            schemas.append({
                "name": name,
                "description": description,
                "parameters": params
            })
    return schemas

def execute_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate tool arguments with Pydantic and execute the tool.
    Returns error dict if validation or execution fails (enabling self-correction).
    """
    if name not in TOOL_DEFINITIONS:
        return {"error": f"Unknown tool '{name}'", "available_tools": list(TOOL_DEFINITIONS.keys())}

    func, validator_cls, _, _ = TOOL_DEFINITIONS[name]

    # Strict validation of tool arguments
    try:
        validated = validator_cls(**arguments)
        valid_kwargs = validated.model_dump()
    except ValidationError as ve:
        err_msg = f"Argument validation failed for tool '{name}': {ve.errors()}"
        logger.warning(err_msg)
        return {"error": err_msg, "status": "validation_error"}

    # Tool execution
    try:
        # Filter kwargs to match function signature
        return func(**valid_kwargs)
    except Exception as e:
        err_msg = f"Execution error in tool '{name}': {str(e)}"
        logger.error(err_msg)
        return {"error": err_msg, "status": "execution_error"}

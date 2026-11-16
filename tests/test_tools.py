"""Unit tests for tool execution, registry, and Planner Agent loop."""
import pytest
import asyncio
from datetime import date, timedelta
from app.db import init_db, seed_demo_data, get_connection, set_current_llm_mode
from app.tools.registry import execute_tool, get_tools_schema
from app.agents.planner import PlannerAgent

@pytest.fixture(autouse=True)
def setup_test_db():
    init_db()
    seed_demo_data(force=True)
    set_current_llm_mode("mock")

def test_tool_registry_schemas():
    """Verify all 12 tools are declared in schema registry."""
    schemas = get_tools_schema()
    names = [s["name"] for s in schemas]
    expected_tools = [
        "parse_syllabus", "estimate_difficulty", "get_free_slots",
        "create_schedule", "check_conflicts", "add_revision_sessions",
        "mark_progress", "replan", "compress_plan", "generate_quiz",
        "record_quiz_result", "send_reminder"
    ]
    for t in expected_tools:
        assert t in names, f"Tool '{t}' missing from tool registry"

def test_execute_tool_success_and_error_handling():
    """Verify tool execution returns JSON-serializable dict and catches bad tools."""
    today = date.today().isoformat()
    end_date = (date.today() + timedelta(days=7)).isoformat()

    # Valid execution
    res = execute_tool("get_free_slots", {"start_date": today, "end_date": end_date})
    assert "days" in res
    assert len(res["days"]) > 0

    # Invalid tool name
    bad_res = execute_tool("non_existent_tool", {})
    assert "error" in bad_res

    # Validation error caught cleanly
    invalid_arg_res = execute_tool("get_free_slots", {"start_date": "bad_date", "end_date": "bad_date"})
    assert "error" in invalid_arg_res
    assert invalid_arg_res["status"] == "validation_error"

def test_spaced_repetition_tool():
    """Verify add_revision_sessions tool creates spaced revision slots."""
    today = date.today().isoformat()
    end_date = (date.today() + timedelta(days=21)).isoformat()

    # Create initial schedule
    execute_tool("create_schedule", {"start_date": today, "end_date": end_date})

    with get_connection() as conn:
        task = conn.execute("SELECT id FROM tasks LIMIT 1;").fetchone()
        task_id = task["id"]

    rev_res = execute_tool("add_revision_sessions", {"task_id": task_id})
    assert rev_res["status"] == "success"
    assert rev_res["revision_sessions_created"] > 0
    assert len(rev_res["sessions"]) > 0

def test_weak_topic_quiz_remedial_loop():
    """Verify recording a quiz score < 60% automatically adds a remedial revision session."""
    with get_connection() as conn:
        topic = conn.execute("SELECT id FROM topics LIMIT 1;").fetchone()
        topic_id = topic["id"]

    # Record low score
    quiz_res = execute_tool("record_quiz_result", {"topic_id": topic_id, "score": 40.0})
    assert quiz_res["status"] == "success"
    assert quiz_res["weak_topic_loop"] is True
    assert quiz_res["revision_task_id"] is not None

def test_planner_agent_run_loop():
    """Verify Planner Agent autonomous run executes tools and logs to agent_logs."""
    agent = PlannerAgent()
    result = asyncio.run(agent.run())

    assert result["status"] == "success"
    assert result["iterations"] >= 3
    assert result["run_id"] == agent.run_id

    # Verify agent logs recorded in SQLite
    with get_connection() as conn:
        logs = conn.execute("SELECT * FROM agent_logs WHERE run_id = ? ORDER BY step;", (agent.run_id,)).fetchall()
        assert len(logs) >= 3
        tool_names = [l["tool_name"] for l in logs]
        assert "get_free_slots" in tool_names
        assert "create_schedule" in tool_names
        assert "check_conflicts" in tool_names

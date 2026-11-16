"""Tests for Part A (Mock/Live LLM Toggle) and Part B (Demo vs Real Data & New Plan Wizard)."""
import pytest
from datetime import date, timedelta
from starlette.testclient import TestClient
from app.main import app
import app.main as main_module
from app.db import (
    init_db, seed_demo_data,
    get_current_llm_mode, set_current_llm_mode,
    get_is_demo
)

@pytest.fixture(autouse=True)
def setup_db():
    init_db()
    seed_demo_data(force=True)
    yield

def test_get_and_switch_llm_mode():
    client = TestClient(app)
    # Get current mode
    res = client.get("/api/llm/mode")
    assert res.status_code == 200
    data = res.json()
    assert "mode" in data
    assert "live_available" in data
    assert "model" in data
    assert "reason" in data

    # Switch to mock
    res = client.post("/api/llm/mode", json={"mode": "mock"})
    assert res.status_code == 200
    assert res.json()["mode"] == "mock"
    assert get_current_llm_mode() == "mock"

    # Verify GET reflects mock
    res = client.get("/api/llm/mode")
    assert res.json()["mode"] == "mock"

    # Switch back if live is available
    if data["live_available"]:
        res = client.post("/api/llm/mode", json={"mode": "live"})
        assert res.status_code == 200
        assert res.json()["mode"] == "live"
        assert get_current_llm_mode() == "live"

def test_live_mode_rejected_without_api_key(monkeypatch):
    client = TestClient(app)
    # Ensure current mode is mock
    set_current_llm_mode("mock")

    # Simulate missing API key
    monkeypatch.setattr(main_module, "GEMINI_API_KEY", "")
    
    res = client.post("/api/llm/mode", json={"mode": "live"})
    assert res.status_code == 400
    assert "Add GEMINI_API_KEY to .env" in res.json()["detail"]
    assert get_current_llm_mode() == "mock"

def test_per_run_fallback_does_not_change_global_mode(monkeypatch):
    """When a live run falls back to mock due to error, the global mode remains unchanged."""
    monkeypatch.setenv("GEMINI_API_KEY", "dummy_test_api_key_for_testing")
    monkeypatch.setattr("app.config.GEMINI_API_KEY", "dummy_test_api_key_for_testing")
    monkeypatch.setattr("app.main.GEMINI_API_KEY", "dummy_test_api_key_for_testing")
    monkeypatch.setattr("app.llm.gemini.GEMINI_API_KEY", "dummy_test_api_key_for_testing")
    monkeypatch.setattr("app.db.GEMINI_API_KEY", "dummy_test_api_key_for_testing", raising=False)

    try:
        set_current_llm_mode("live")
        assert get_current_llm_mode() == "live"

        from app.llm.gemini import get_llm_provider, GeminiProvider
        provider = get_llm_provider()
        assert isinstance(provider, GeminiProvider)

        class BrokenModels:
            @staticmethod
            def generate_content(*args, **kwargs):
                raise RuntimeError("API quota exceeded")

        class BrokenClient:
            models = BrokenModels()

        monkeypatch.setattr(provider, "_client", BrokenClient())

        resp = provider.generate_with_tools(
            messages=[{"role": "user", "content": "Hello"}],
            tools_schema=[],
            system_prompt="Test system"
        )
        assert resp.is_fallback is True
        assert "API quota exceeded" in (resp.fallback_reason or "")
        # Global mode must still be "live"!
        assert get_current_llm_mode() == "live"
    finally:
        set_current_llm_mode("mock")

def test_empty_start_and_new_plan_reset():
    client = TestClient(app)
    
    # After seed, is_demo is True and counts > 0
    status = client.get("/api/status").json()
    assert status["is_demo"] is True
    assert status["subjects_count"] > 0

    # Start new plan
    res = client.post("/api/plan/new")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"

    # Verify completely empty
    status = client.get("/api/status").json()
    assert status["is_demo"] is False
    assert status["subjects_count"] == 0
    assert status["topics_count"] == 0
    assert status["tasks_count"] == 0

def test_backup_export():
    client = TestClient(app)
    res = client.get("/api/backup")
    assert res.status_code == 200
    data = res.json()
    assert "exported_at" in data
    assert "subjects" in data
    assert len(data["subjects"]) >= 4

def test_wizard_validation_rules():
    client = TestClient(app)
    # 1. Empty subjects list rejected
    res = client.post("/api/setup", json={
        "profile": {
            "name": "Test Student",
            "max_daily_hours": 4.0,
            "peak_energy": "morning",
            "preferred_break_min": 15,
            "burnout_threshold_days": 5
        },
        "subjects": []
    })
    assert res.status_code == 422

    # 2. Subject with 0 topics rejected
    res = client.post("/api/setup", json={
        "profile": {
            "name": "Test Student",
            "max_daily_hours": 4.0,
            "peak_energy": "morning",
            "preferred_break_min": 15,
            "burnout_threshold_days": 5
        },
        "subjects": [
            {
                "name": "Physics",
                "exam_date": (date.today() + timedelta(days=10)).isoformat(),
                "priority": 1,
                "color": "#3B82F6",
                "topics": []
            }
        ]
    })
    assert res.status_code == 422

    # 3. Valid setup succeeds and sets is_demo = False
    res = client.post("/api/setup", json={
        "profile": {
            "name": "Valid Student",
            "max_daily_hours": 4.0,
            "peak_energy": "morning",
            "preferred_break_min": 15,
            "burnout_threshold_days": 5
        },
        "subjects": [
            {
                "name": "Physics",
                "exam_date": (date.today() + timedelta(days=10)).isoformat(),
                "priority": 1,
                "color": "#3B82F6",
                "topics": [
                    {
                        "unit": "Unit 1",
                        "name": "Mechanics",
                        "difficulty": 3,
                        "est_hours": 2.0,
                        "weightage": 1.0
                    }
                ]
            }
        ]
    })
    assert res.status_code == 200
    assert get_is_demo() is False

def test_subject_crud_and_exam_date_replan():
    client = TestClient(app)
    
    # 1. Fetch subjects
    subjects = client.get("/api/subjects").json()
    assert len(subjects) > 0
    first_subj = subjects[0]
    subj_id = first_subj["id"]

    # 2. Update subject exam date (shifts by 3 days)
    new_date = (date.today() + timedelta(days=35)).isoformat()
    assert first_subj["exam_date"] != new_date
    
    res = client.put(f"/api/subjects/{subj_id}", json={
        "name": first_subj["name"],
        "exam_date": new_date,
        "priority": first_subj["priority"],
        "color": first_subj["color"]
    })
    assert res.status_code == 200
    data = res.json()
    assert data["exam_date_changed"] is True
    assert "replan_details" in data

    # 3. Add new topic to this subject
    res = client.post("/api/topics", json={
        "subject_id": subj_id,
        "unit": "Unit Extra",
        "name": "Advanced Quantum Theory",
        "difficulty": 4,
        "est_hours": 3.0,
        "weightage": 1.2
    })
    assert res.status_code == 200
    new_topic_id = res.json()["topic"]["id"]

    # 4. Update topic
    res = client.put(f"/api/topics/{new_topic_id}", json={
        "name": "Advanced Quantum Theory Revised",
        "difficulty": 5,
        "est_hours": 3.5,
        "weightage": 1.5,
        "mastery": 0.5,
        "status": "in_progress"
    })
    assert res.status_code == 200
    assert res.json()["topic"]["name"] == "Advanced Quantum Theory Revised"

    # 5. Delete topic
    res = client.delete(f"/api/topics/{new_topic_id}")
    assert res.status_code == 200
    assert res.json()["status"] == "success"

def test_rule_based_syllabus_parser_offline():
    from app.tools.syllabus import rule_based_syllabus_parser
    text = """
    Unit 1: Fundamentals of AI
    - Search algorithms
    - Heuristics and A* search
    Unit 2: Knowledge Representation
    * Propositional logic
    * First order logic
    """
    parsed = rule_based_syllabus_parser(text, "Artificial Intelligence")
    assert parsed["subject"] == "Artificial Intelligence"
    assert len(parsed["topics"]) >= 2
    all_topics = [t["name"] for t in parsed["topics"]]
    assert "Search algorithms" in all_topics
    assert "Propositional logic" in all_topics

def test_semicolon_and_unit_header_syllabus_parser():
    set_current_llm_mode("mock")
    from app.tools.syllabus import parse_syllabus_internal
    text = (
        "6.2 Applications: Predictive modeling – House price prediction, Fraud detection; "
        "Clustering – Customer segmentation; Use cases in healthcare; Time series – Weather forecasting; "
        "Recommendation engines – Product recommendation; Ethics, bias, and privacy in data science; "
        "Reproducible notebooks and version control using Git."
    )
    res = parse_syllabus_internal(text=text, subject_name="Genrative AI", save_to_db=False)
    assert res["status"] == "success"
    assert "topics" in res
    assert len(res["topics"]) == 7
    topic_names = [t["name"] for t in res["topics"]]
    assert any("Predictive modeling" in name for name in topic_names)
    assert any("Recommendation engines" in name for name in topic_names)
    assert any("Ethics, bias" in name for name in topic_names)

def test_gemini_503_fast_fallback(monkeypatch):
    """Verify that when Gemini raises 503 UNAVAILABLE, GeminiProvider immediately falls back without 5-minute tenacity hangs."""
    from app.llm.gemini import GeminiProvider
    provider = GeminiProvider(api_key="mock_key_test", model="gemini-3.8-flash")

    class MockModels:
        def generate_content(self, *args, **kwargs):
            raise Exception("503 UNAVAILABLE. This model is currently experiencing high demand.")

    class MockClient:
        models = MockModels()

    monkeypatch.setattr(provider, "_client", MockClient())

    # Test generate()
    resp = provider.generate("Give study advice")
    assert resp is not None
    assert len(resp) > 0

    # Test generate_with_tools()
    from app.tools.registry import get_tools_schema
    tools = get_tools_schema()
    resp_tools = provider.generate_with_tools(
        messages=[{"role": "user", "content": "Schedule my subjects"}],
        tools_schema=tools
    )
    assert resp_tools.is_fallback is True
    assert "503" in (resp_tools.fallback_reason or "")
    assert len(resp_tools.tool_calls) > 0 or resp_tools.content is not None

@pytest.mark.anyio
async def test_planner_agent_run_async():
    """Verify PlannerAgent.run() executes asynchronously without blocking the event loop."""
    set_current_llm_mode("mock")
    from app.agents.planner import PlannerAgent, AGENT_RUN_HISTORY
    agent = PlannerAgent()
    res = await agent.run()
    assert res["status"] == "success"
    assert res["run_id"] == agent.run_id
    # Verify events were recorded in history
    assert agent.run_id in AGENT_RUN_HISTORY
    events = AGENT_RUN_HISTORY[agent.run_id]
    event_types = [e["type"] for e in events]
    assert "start" in event_types
    assert "iteration" in event_types
    assert "done" in event_types



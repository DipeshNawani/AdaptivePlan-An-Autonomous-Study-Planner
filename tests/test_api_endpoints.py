"""End-to-end API integration tests for AdaptivePlan endpoints."""
import pytest
from starlette.testclient import TestClient
from app.main import app
from app.db import init_db, seed_demo_data, get_connection, set_current_llm_mode
from datetime import date, timedelta

@pytest.fixture(autouse=True)
def setup_test_db():
    init_db()
    seed_demo_data(force=True)
    set_current_llm_mode("mock")

@pytest.fixture
def client():
    return TestClient(app)

def test_api_status(client):
    res = client.get("/api/status")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "online"
    assert data["subjects_count"] >= 4
    assert data["topics_count"] >= 16

def test_api_setup_data(client):
    res = client.get("/api/setup/data")
    assert res.status_code == 200
    data = res.json()
    assert "profile" in data
    assert "free_slots" in data
    assert len(data["subjects"]) >= 4

def test_generate_and_get_plan(client):
    # Trigger plan generation
    gen_res = client.post("/api/plan/generate")
    assert gen_res.status_code == 200
    gen_data = gen_res.json()
    assert gen_data["status"] == "started"
    assert "run_id" in gen_data

    # Synchronously populate schedule via internal tool to test /api/plan
    today = date.today().isoformat()
    end_d = (date.today() + timedelta(days=21)).isoformat()
    from app.tools.scheduling import create_schedule_internal
    create_schedule_internal(today, end_d)

    # Fetch plan
    plan_res = client.get("/api/plan")
    assert plan_res.status_code == 200
    tasks = plan_res.json()["tasks"]
    assert len(tasks) > 0
    t = tasks[0]
    assert "subject_name" in t
    assert "topic_name" in t
    assert "duration_hours" in t

def test_task_status_and_explain(client):
    today = date.today().isoformat()
    end_d = (date.today() + timedelta(days=21)).isoformat()
    from app.tools.scheduling import create_schedule_internal
    create_schedule_internal(today, end_d)

    with get_connection() as conn:
        task_id = conn.execute("SELECT id FROM tasks LIMIT 1;").fetchone()["id"]

    # Explain task
    exp_res = client.get(f"/api/tasks/{task_id}/explain")
    assert exp_res.status_code == 200
    exp_data = exp_res.json()
    assert "priority_factors" in exp_data
    assert "reasoning" in exp_data

    # Mark Done
    status_res = client.post(f"/api/tasks/{task_id}/status", json={"status": "done"})
    assert status_res.status_code == 200
    assert status_res.json()["new_status"] == "done"

def test_dashboard_and_coach(client):
    today = date.today().isoformat()
    end_d = (date.today() + timedelta(days=21)).isoformat()
    from app.tools.scheduling import create_schedule_internal
    create_schedule_internal(today, end_d)

    # Dashboard
    dash_res = client.get("/api/dashboard")
    assert dash_res.status_code == 200
    dash_data = dash_res.json()
    assert len(dash_data["readiness"]) >= 4
    assert "summary" in dash_data

    # Coach feedback
    coach_res = client.get("/api/coach")
    assert coach_res.status_code == 200
    coach_data = coach_res.json()
    assert "score" in coach_data
    assert "encouragement" in coach_data

def test_daily_checkin(client):
    checkin_res = client.get("/api/checkin")
    assert checkin_res.status_code == 200
    assert "message" in checkin_res.json()

def test_quiz_flow(client):
    with get_connection() as conn:
        topic_id = conn.execute("SELECT id FROM topics LIMIT 1;").fetchone()["id"]

    # Generate quiz
    gen_res = client.post("/api/quiz/generate", json={"topic_id": topic_id, "n": 3})
    assert gen_res.status_code == 200
    quiz_data = gen_res.json()
    assert len(quiz_data["questions"]) >= 2
    questions = quiz_data["questions"]

    # Submit quiz
    answers = [q["options"][0] for q in questions]
    sub_res = client.post("/api/quiz/submit", json={
        "topic_id": topic_id,
        "answers": answers,
        "questions": questions
    })
    assert sub_res.status_code == 200
    res_data = sub_res.json()
    assert "score" in res_data
    assert "new_mastery" in res_data

def test_quiz_answers_match_with_various_letter_and_text_formats(client):
    """Ensure full option texts match even if answer is a single letter or has differing prefixes."""
    with get_connection() as conn:
        topic_id = conn.execute("SELECT id FROM topics LIMIT 1;").fetchone()["id"]

    test_questions = [
        {
            "question": "What structure connects blocks in a blockchain?",
            "options": [
                "A. Merkle Tree hashing",
                "B. Cryptographic hash linking",
                "C. Asymmetric encryption",
                "D. Distributed consensus mechanism"
            ],
            # LLM outputting single letter 'B'
            "answer": "B",
            "explanation": "Cryptographic hash linking connects blocks."
        },
        {
            "question": "What is an optimal search algorithm?",
            "options": [
                "Depth-First Search",
                "Breadth-First Search",
                "A* Search",
                "Hill Climbing"
            ],
            # LLM outputting text
            "answer": "A* Search",
            "explanation": "A* is optimal with admissible heuristic."
        },
        {
            "question": "What type of network is Bitcoin?",
            "options": [
                "A) Centralized",
                "B) Decentralized Peer-to-Peer",
                "C) Client-Server",
                "D) Monolithic"
            ],
            # LLM outputting 'B) Decentralized Peer-to-Peer'
            "answer": "B) Decentralized Peer-to-Peer",
            "explanation": "Bitcoin operates as a P2P network."
        }
    ]

    # User answers by clicking options in the UI (which gives the exact option string value)
    user_answers = [
        "B. Cryptographic hash linking", # matches Q1's answer "B"
        "A* Search",                     # matches Q2's answer "A* Search"
        "B) Decentralized Peer-to-Peer"  # matches Q3's answer
    ]

    sub_res = client.post("/api/quiz/submit", json={
        "topic_id": topic_id,
        "answers": user_answers,
        "questions": test_questions
    })
    assert sub_res.status_code == 200
    res_data = sub_res.json()
    assert res_data["score"] == 100.0
    assert res_data["correct_count"] == 3
    assert res_data["total_count"] == 3
    for qr in res_data["question_results"]:
        assert qr["is_correct"] is True, f"Failed on question: {qr['question']}"

def test_quiz_grading_with_same_letter_options(client):
    """
    Ensure two options starting with the same letter (Create/Compile, Chain/Cache, TCP/ARP)
    are graded correctly for every correct/chosen pair:
    - by full text
    - by letter ('A', 'B', 'A.', 'B)')
    - by 'C. text' ('A. text', 'B. text')
    """
    from app.tools.quiz import check_quiz_answer

    test_cases = [
        # Pair 1: Create vs Compile
        {
            "options": ["Create virtual machines", "Compile source code", "Manage memory", "Route traffic"],
            "prefixed_options": ["A. Create virtual machines", "B. Compile source code", "C. Manage memory", "D. Route traffic"]
        },
        # Pair 2: Chain vs Cache
        {
            "options": ["Chain validation", "Cache invalidation", "Disk storage", "Process scheduling"],
            "prefixed_options": ["A. Chain validation", "B. Cache invalidation", "C. Disk storage", "D. Process scheduling"]
        },
        # Pair 3: TCP vs ARP
        {
            "options": ["TCP", "ARP", "DNS", "BGP"],
            "prefixed_options": ["A. TCP", "B. ARP", "C. DNS", "D. BGP"]
        }
    ]

    for tc in test_cases:
        opts = tc["options"]
        pref_opts = tc["prefixed_options"]
        opt_a = opts[0]
        opt_b = opts[1]
        pref_a = pref_opts[0]
        pref_b = pref_opts[1]

        # 1. By full text:
        assert check_quiz_answer(opts, opt_a, opt_a) is True
        assert check_quiz_answer(opts, opt_a, opt_b) is False
        assert check_quiz_answer(opts, opt_b, opt_b) is True
        assert check_quiz_answer(opts, opt_b, opt_a) is False

        # 2. By letter:
        assert check_quiz_answer(opts, opt_a, "A") is True
        assert check_quiz_answer(opts, opt_a, "B") is False
        assert check_quiz_answer(opts, opt_b, "B") is True
        assert check_quiz_answer(opts, opt_b, "A") is False
        # With trailing punctuation
        assert check_quiz_answer(opts, opt_a, "A.") is True
        assert check_quiz_answer(opts, opt_a, "B)") is False
        assert check_quiz_answer(opts, opt_b, "B.") is True
        assert check_quiz_answer(opts, opt_b, "A)") is False

        # When stored answer is letter:
        assert check_quiz_answer(opts, "A", opt_a) is True
        assert check_quiz_answer(opts, "A", opt_b) is False
        assert check_quiz_answer(opts, "B", opt_b) is True
        assert check_quiz_answer(opts, "B", opt_a) is False

        # 3. By 'C. text' prefix format:
        assert check_quiz_answer(pref_opts, pref_a, pref_a) is True
        assert check_quiz_answer(pref_opts, pref_a, pref_b) is False
        assert check_quiz_answer(pref_opts, pref_b, pref_b) is True
        assert check_quiz_answer(pref_opts, pref_b, pref_a) is False
        # User submits full prefix, stored answer is plain text
        assert check_quiz_answer(pref_opts, opt_a, pref_a) is True
        assert check_quiz_answer(pref_opts, opt_a, pref_b) is False
        assert check_quiz_answer(pref_opts, opt_b, pref_b) is True
        assert check_quiz_answer(pref_opts, opt_b, pref_a) is False
        # User submits plain text, stored answer has prefix
        assert check_quiz_answer(pref_opts, pref_a, opt_a) is True
        assert check_quiz_answer(pref_opts, pref_a, opt_b) is False
        assert check_quiz_answer(pref_opts, pref_b, opt_b) is True
        assert check_quiz_answer(pref_opts, pref_b, opt_a) is False

    # Also verify via /api/quiz/submit endpoint
    with get_connection() as conn:
        topic_id = conn.execute("SELECT id FROM topics LIMIT 1;").fetchone()["id"]

    submit_questions = [
        {
            "question": "Which task is CPU-intensive?",
            "options": ["Create virtual machines", "Compile source code", "Manage memory", "Route traffic"],
            "answer": "Compile source code",
            "explanation": "Compilation is CPU-bound."
        },
        {
            "question": "Which mechanism ensures block validation?",
            "options": ["Chain validation", "Cache invalidation", "Disk storage", "Process scheduling"],
            "answer": "A",
            "explanation": "Chain validation checks block links."
        },
        {
            "question": "Which protocol resolves IP to MAC?",
            "options": ["A. TCP", "B. ARP", "C. DNS", "D. BGP"],
            "answer": "B. ARP",
            "explanation": "ARP maps IP addresses to physical MAC addresses."
        }
    ]

    # Test exact correct answers via text, letter, and prefix
    sub_res = client.post("/api/quiz/submit", json={
        "topic_id": topic_id,
        "answers": ["Compile source code", "A", "B. ARP"],
        "questions": submit_questions
    })
    assert sub_res.status_code == 200
    res_data = sub_res.json()
    assert res_data["score"] == 100.0
    assert res_data["correct_count"] == 3

    # Test choosing the other same-letter option (must be graded incorrect)
    sub_res_wrong = client.post("/api/quiz/submit", json={
        "topic_id": topic_id,
        "answers": ["Create virtual machines", "B", "A. TCP"],
        "questions": submit_questions
    })
    assert sub_res_wrong.status_code == 200
    res_data_wrong = sub_res_wrong.json()
    assert res_data_wrong["score"] == 0.0
    assert res_data_wrong["correct_count"] == 0

def test_export_endpoints(client):
    today = date.today().isoformat()
    end_d = (date.today() + timedelta(days=21)).isoformat()
    from app.tools.scheduling import create_schedule_internal
    create_schedule_internal(today, end_d)

    # Export ICS
    ics_res = client.get("/api/export/ics")
    assert ics_res.status_code == 200
    assert ics_res.headers["content-type"].startswith("text/calendar")
    assert "BEGIN:VCALENDAR" in ics_res.text

    # Export PDF
    pdf_res = client.get("/api/export/pdf")
    assert pdf_res.status_code == 200
    assert pdf_res.headers["content-type"] == "application/pdf"
    assert len(pdf_res.content) > 1000

def test_panic_mode_endpoint(client):
    with get_connection() as conn:
        s_id = conn.execute("SELECT id FROM subjects LIMIT 1;").fetchone()["id"]

    panic_res = client.post("/api/plan/panic", json={"subject_id": s_id, "days_left": 3})
    assert panic_res.status_code == 200
    assert panic_res.json()["compressed"] is True

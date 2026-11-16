"""Quiz generation, automated grading, and weak-topic feedback loop."""
import json
import logging
import re
from datetime import date, timedelta
from typing import Dict, Any, List, Optional, Tuple
from app.db import get_db, get_connection
from app.llm.gemini import get_llm_provider

logger = logging.getLogger(__name__)

def normalize_quiz_text(text: str) -> str:
    """Normalize text: strip, lowercase, trim trailing punctuation, collapse multiple spaces."""
    if not text:
        return ""
    t = text.strip().strip('"\'').strip()
    t = t.rstrip('.?,;:!').strip().lower()
    return re.sub(r'\s+', ' ', t)

def as_letter_index(text: str) -> Optional[int]:
    """
    If text is exactly one character A-D (optionally with trailing '.' or ')'),
    return its 0-based index 0..3. Otherwise return None.
    Never treats full words (like 'Compile', 'Create', 'TCP') as letters.
    """
    if not text:
        return None
    cleaned = text.strip()
    m = re.fullmatch(r'([A-Da-d])[\.\)]?', cleaned)
    if m:
        letter = m.group(1).upper()
        return ord(letter) - ord('A')
    return None

def parse_option_prefix_and_text(s: str) -> Tuple[Optional[str], str]:
    """
    If string starts with an explicit option prefix like 'A. ', 'A) ', 'A: ', 'A - ',
    return (letter, text). Otherwise return (None, s).
    Only matches when a single letter A-D is followed by punctuation and space.
    """
    if not s:
        return None, ""
    cleaned = s.strip()
    m = re.match(r'^([A-Da-d])[\.\)\:\-]\s+(.+)$', cleaned)
    if m:
        return m.group(1).upper(), m.group(2).strip()
    return None, cleaned

def check_quiz_answer(options: List[str], q_answer: str, user_answer: str) -> bool:
    """
    Simple, safe comparison:
    1. Compare user's answer directly with stored answer text (after normalization). Equal means correct.
    2. Treat user's answer (or stored answer) as an option LETTER only if it is exactly one character A-D
       (optionally with a trailing '.' or ')'), mapped to options[index].
    3. If an answer or option has an explicit prefix like 'C. text', compare the inner text as well.
    4. Never treats the first letter of an option's text as its letter.
    """
    if not user_answer or not q_answer:
        return False

    u_norm = normalize_quiz_text(user_answer)
    a_norm = normalize_quiz_text(q_answer)
    if u_norm == a_norm:
        return True

    # Map user_answer if it is strictly an option letter (A-D)
    u_idx = as_letter_index(user_answer)
    u_mapped = options[u_idx] if (u_idx is not None and 0 <= u_idx < len(options)) else user_answer

    # Map q_answer if it is strictly an option letter (A-D)
    a_idx = as_letter_index(q_answer)
    a_mapped = options[a_idx] if (a_idx is not None and 0 <= a_idx < len(options)) else q_answer

    # Compare mapped answers
    if normalize_quiz_text(u_mapped) == normalize_quiz_text(a_mapped):
        return True

    # Check explicit 'C. text' prefix format
    u_pref, u_text = parse_option_prefix_and_text(u_mapped)
    a_pref, a_text = parse_option_prefix_and_text(a_mapped)

    # If both have explicit different prefixes (e.g. 'A. ' vs 'B. '), they cannot match
    if u_pref and a_pref and u_pref != a_pref:
        return False

    if normalize_quiz_text(u_text) == normalize_quiz_text(a_text):
        return True

    return False

def resolve_correct_answer_display(options: List[str], q_answer: str) -> str:
    """
    Return display string for correct answer. If q_answer is an option letter (A-D),
    expands to the full option text from options.
    """
    if not q_answer:
        return ""
    idx = as_letter_index(q_answer)
    if idx is not None and 0 <= idx < len(options):
        return options[idx]
    for opt in options:
        if normalize_quiz_text(opt) == normalize_quiz_text(q_answer):
            return opt
        _, opt_text = parse_option_prefix_and_text(opt)
        if normalize_quiz_text(opt_text) == normalize_quiz_text(q_answer):
            return opt
    return q_answer

def generate_quiz_internal(topic_id: int, n: int = 3) -> Dict[str, Any]:
    """
    Generate n multiple choice questions for a topic using LLM.
    """
    with get_connection() as conn:
        topic = conn.execute("""
        SELECT t.name as topic_name, t.unit, s.name as subject_name
        FROM topics t
        JOIN subjects s ON t.subject_id = s.id
        WHERE t.id = ?;
        """, (topic_id,)).fetchone()

        if not topic:
            raise ValueError(f"Topic #{topic_id} not found")

    prompt = (
        f"Generate {n} multiple-choice exam questions for engineering students on the topic '{topic['topic_name']}' "
        f"from {topic['subject_name']} ({topic['unit']}). "
        "Each question must include 4 distinct options and the exact correct answer text. "
        "Return strictly valid JSON format: "
        '{"questions": [{"question": "...", "options": ["Option 1", "Option 2", "Option 3", "Option 4"], "answer": "Option 1", "explanation": "..."}]}'
    )

    llm = get_llm_provider()
    resp_text = llm.generate(prompt, system_prompt="You are an engineering exam creator. Output strictly JSON.")

    try:
        cleaned = resp_text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        data = json.loads(cleaned.strip())
        questions = data.get("questions", [])
        for q in questions:
            opts = q.get("options", [])
            raw_ans = q.get("answer", "")
            q["answer"] = resolve_correct_answer_display(opts, raw_ans)
    except Exception as e:
        logger.warning(f"Fallback to default quiz questions due to parsing: {e}")
        questions = [
            {
                "question": f"What is the foundational principle underlying {topic['topic_name']}?",
                "options": ["Deterministic Convergence", "State Space Reduction", "Empirical Heuristic", "Divide & Conquer"],
                "answer": "State Space Reduction",
                "explanation": f"Key conceptual objective in {topic['topic_name']} is optimizing computational state representation."
            },
            {
                "question": f"Which metric is most critical when evaluating {topic['topic_name']} performance?",
                "options": ["Latency & Throughput", "Source Code Lines", "User Font Size", "Storage Media"],
                "answer": "Latency & Throughput",
                "explanation": "Standard performance profiling depends directly on time complexity and throughput constraints."
            }
        ]

    return {
        "status": "success",
        "topic_id": topic_id,
        "topic_name": topic["topic_name"],
        "questions": questions[:n]
    }

def record_quiz_result_internal(topic_id: int, score: float, total: int = 3, correct: int = 2) -> Dict[str, Any]:
    """
    Record quiz results, update topic mastery score, and trigger weak-topic revision loop.
    If score < 60%, automatically schedules an extra revision task.
    """
    if not (0.0 <= score <= 100.0):
        raise ValueError("Score must be between 0.0 and 100.0")

    weak_topic_triggered = False
    new_revision_task_id = None

    with get_db() as conn:
        topic = conn.execute("SELECT id, name, mastery, subject_id FROM topics WHERE id = ?;", (topic_id,)).fetchone()
        if not topic:
            raise ValueError(f"Topic #{topic_id} not found")

        # Save to quiz_results
        conn.execute("""
        INSERT INTO quiz_results (topic_id, score, total_questions, correct_answers)
        VALUES (?, ?, ?, ?);
        """, (topic_id, score, total, correct))

        # Update mastery score (weighted moving average)
        old_mastery = topic["mastery"] or 0.0
        normalized_score = score / 100.0
        new_mastery = round((old_mastery * 0.4) + (normalized_score * 0.6), 2)

        conn.execute("UPDATE topics SET mastery = ? WHERE id = ?;", (new_mastery, topic_id))

        # Weak topic loop: if score < 60%, schedule urgent revision within 48 hours
        if score < 60.0:
            weak_topic_triggered = True
            subj = conn.execute("SELECT exam_date FROM subjects WHERE id = ?;", (topic["subject_id"],)).fetchone()
            exam_d = date.fromisoformat(subj["exam_date"])
            today = date.today()
            rev_date = today + timedelta(days=2)
            if rev_date >= exam_d:
                rev_date = today + timedelta(days=1)

            cur = conn.execute("""
            INSERT INTO tasks (topic_id, type, date, start_time, end_time, duration_hours, status, reason, priority_score)
            VALUES (?, 'revision', ?, '19:00', '20:30', 1.5, 'pending', ?, 3.5);
            """, (topic_id, rev_date.isoformat(), f"Weak-Topic Remediation: Quiz score was {score:.0f}%"))
            new_revision_task_id = cur.lastrowid

    return {
        "status": "success",
        "topic_id": topic_id,
        "score": score,
        "new_mastery": new_mastery,
        "weak_topic_loop": weak_topic_triggered,
        "revision_task_id": new_revision_task_id,
        "feedback": "Great mastery!" if score >= 80 else ("Adequate understanding." if score >= 60 else "Weak topic detected. Remedial revision session automatically added.")
    }

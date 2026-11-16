"""Exam Readiness Score engine (pedagogical heuristic metric)."""
from datetime import date
from typing import Dict, Any, List
from app.db import get_connection

def calculate_readiness_for_subject(subject_id: int) -> Dict[str, Any]:
    """
    Compute Exam Readiness Score (0-100) for a subject.
    
    Heuristic weighting breakdown:
    - 40% Completion Ratio: fraction of topics marked completed
    - 30% Topic Mastery: average mastery score from quizzes & practice
    - 20% Revision Ratio: presence of spaced repetition revision tasks
    - 10% Urgency Buffer: penalty if exam is extremely near with unfinished units
    
    Note for Viva: These weights (0.4 / 0.3 / 0.2 / 0.1) are pedagogical heuristics
    designed for cognitive pacing, rather than an empirical constant.
    """
    with get_connection() as conn:
        subj = conn.execute("SELECT id, name, exam_date, color FROM subjects WHERE id = ?;", (subject_id,)).fetchone()
        if not subj:
            raise ValueError(f"Subject #{subject_id} not found")

        topics = conn.execute("""
        SELECT id, name, mastery, status, est_hours
        FROM topics
        WHERE subject_id = ?;
        """, (subject_id,)).fetchall()

        if not topics:
            return {
                "subject_id": subject_id,
                "subject_name": subj["name"],
                "color": subj["color"],
                "exam_date": subj["exam_date"],
                "readiness_score": 0,
                "days_left": (date.fromisoformat(subj["exam_date"]) - date.today()).days,
                "topics_count": 0,
                "completed_count": 0,
                "avg_mastery": 0.0
            }

        total_topics = len(topics)
        completed_topics = sum(1 for t in topics if t["status"] == "completed")
        completion_ratio = completed_topics / total_topics

        avg_mastery = sum(t["mastery"] for t in topics) / total_topics

        # Check revision tasks
        rev_count = conn.execute("""
        SELECT COUNT(*) as c
        FROM tasks t
        JOIN topics tp ON t.topic_id = tp.id
        WHERE tp.subject_id = ? AND t.type = 'revision' AND t.status = 'done';
        """, (subject_id,)).fetchone()["c"]
        rev_ratio = min(1.0, rev_count / max(1, total_topics))

        exam_d = date.fromisoformat(subj["exam_date"])
        days_left = max(0, (exam_d - date.today()).days)

        # Urgency buffer: higher score if days left is comfortable or syllabus is complete
        if completion_ratio >= 1.0:
            urgency_factor = 1.0
        elif days_left > 14:
            urgency_factor = 1.0
        elif days_left > 7:
            urgency_factor = 0.7
        elif days_left > 2:
            urgency_factor = 0.4
        else:
            urgency_factor = 0.1

        # Calculate final weighted score
        raw_score = (
            (0.40 * completion_ratio) +
            (0.30 * avg_mastery) +
            (0.20 * rev_ratio) +
            (0.10 * urgency_factor)
        ) * 100.0

        readiness = min(100, max(0, int(round(raw_score))))

        return {
            "subject_id": subject_id,
            "subject_name": subj["name"],
            "color": subj["color"],
            "exam_date": subj["exam_date"],
            "days_left": days_left,
            "readiness_score": readiness,
            "completion_ratio": round(completion_ratio * 100, 1),
            "topics_count": total_topics,
            "completed_count": completed_topics,
            "avg_mastery": round(avg_mastery * 100, 1),
            "revisions_done": rev_count
        }

def get_all_readiness_metrics() -> List[Dict[str, Any]]:
    """Retrieve Exam Readiness Scores for all enrolled subjects."""
    with get_connection() as conn:
        subjects = conn.execute("SELECT id FROM subjects ORDER BY exam_date;").fetchall()
    return [calculate_readiness_for_subject(s["id"]) for s in subjects]

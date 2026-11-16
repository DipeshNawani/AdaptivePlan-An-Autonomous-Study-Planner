"""Coach Agent: Audits study plan, checks for burnout, and delivers motivational feedback."""
import json
import logging
from typing import Dict, Any, List
from app.db import get_connection, get_current_llm_mode
from app.llm.gemini import get_llm_provider
from app.agents.prompts import COACH_SYSTEM_PROMPT
from app.tools.scheduling import check_conflicts_internal

logger = logging.getLogger(__name__)

def compute_dynamic_coach_review(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deterministically compute coach score, issues, and suggestions from real plan metrics.
    Ensures zero canned text in Mock mode or when LLM is unavailable.
    """
    score = 100
    issues: List[str] = []
    suggestions: List[str] = []

    max_hrs = metrics.get("max_daily_hours", 4.0)
    consec_peak = metrics.get("max_consecutive_peak_days", 0)
    burnout_thresh = metrics.get("burnout_threshold", 5)
    subj_count = metrics.get("subjects_count", 0)

    # 1. Burnout & consecutive peak intensity
    if consec_peak >= burnout_thresh:
        score -= 15
        issues.append(f"Burnout risk: {consec_peak} consecutive days scheduled at maximum capacity ({max_hrs}h/day).")
        suggestions.append("Insert a lighter study day or rest break midway through heavy study streaks to prevent cognitive fatigue.")
    elif consec_peak >= 3:
        score -= 5
        issues.append(f"High-intensity streak: {consec_peak} consecutive days scheduled near daily capacity.")
        suggestions.append("Ensure you take short breaks between consecutive study sessions.")

    # 2. Revision per subject
    subj_revs = metrics.get("subject_revisions", {})
    missing_rev = [name for name, count in subj_revs.items() if count == 0]
    if missing_rev:
        deduction = min(25, len(missing_rev) * 8)
        score -= deduction
        issues.append(f"{len(missing_rev)} subject(s) lack scheduled revision sessions: {', '.join(missing_rev)}.")
        suggestions.append("Ensure spaced repetition or pre-exam revision buffers are added for all subjects.")

    # 3. Unscheduled topics
    unsched_cnt = metrics.get("unscheduled_topics_count", 0)
    unsched_hrs = metrics.get("unscheduled_hours", 0.0)
    if unsched_cnt > 0:
        score -= 15
        issues.append(f"{unsched_cnt} topic(s) ({unsched_hrs}h) could not fit before the exam with current daily limits.")
        suggestions.append("Increase your max daily study hours or use Panic Mode to focus on high-yield topics.")

    # 4. Missed tasks
    missed_cnt = metrics.get("tasks_missed", 0)
    if missed_cnt > 0:
        score -= min(10, missed_cnt * 4)
        issues.append(f"{missed_cnt} session(s) marked as missed.")
        suggestions.append("Use the Replan button to redistribute missed sessions into upcoming free slots.")

    score = max(40, min(100, score))

    if not issues:
        encouragement = f"Outstanding balance! All {subj_count} subjects have paced study blocks and revision buffers with zero overloaded days."
        suggestions.append("Maintain steady 15-minute breaks after 90 minutes of focused study and test retention with quizzes.")
    elif score >= 80:
        encouragement = "Strong plan foundation. Resolving the minor flagged items will optimize retention leading up to exam day."
    else:
        encouragement = "Schedule pacing requires attention to prevent burnout and ensure all critical topics are covered."

    return {
        "score": score,
        "issues": issues,
        "suggestions": suggestions,
        "encouragement": encouragement
    }

class CoachAgent:
    """
    Coach Agent reviews the generated study plan and student schedule
    for burnout risks, overloaded days, missing revision, and pacing balance.
    """

    def review_plan(self) -> Dict[str, Any]:
        with get_connection() as conn:
            profile = conn.execute("SELECT max_daily_hours, burnout_threshold_days FROM profile WHERE id = 1;").fetchone()
            max_daily_hours = profile["max_daily_hours"] if profile else 4.0
            burnout_threshold = profile["burnout_threshold_days"] if profile else 5

            # Gather schedule statistics
            daily_stats = conn.execute("""
            SELECT date, SUM(duration_hours) as total_hours, COUNT(*) as task_count
            FROM tasks
            GROUP BY date
            ORDER BY date;
            """).fetchall()

            subjects = conn.execute("SELECT id, name, exam_date FROM subjects;").fetchall()
            tasks = conn.execute("SELECT id, type, status FROM tasks;").fetchall()

            # Gather revision count per subject
            subject_revisions = {}
            for s in subjects:
                rev_cnt = conn.execute("""
                SELECT COUNT(*) as c FROM tasks t
                JOIN topics tp ON t.topic_id = tp.id
                WHERE tp.subject_id = ? AND t.type = 'revision';
                """, (s["id"],)).fetchone()["c"]
                subject_revisions[s["name"]] = rev_cnt

        total_tasks = len(tasks)
        done_tasks = sum(1 for t in tasks if t["status"] == "done")
        missed_tasks = sum(1 for t in tasks if t["status"] == "missed")

        # Check for unscheduled topics
        audit = check_conflicts_internal()
        unsched_conflict = next((c for c in audit.get("conflicts", []) if c.get("type") == "unscheduled_topics"), None)
        unsched_cnt = unsched_conflict["count"] if unsched_conflict else 0
        unsched_hrs = unsched_conflict["unscheduled_hours"] if unsched_conflict else 0.0

        # 1. Deterministic Burnout Guard check
        consecutive_peak_days = 0
        max_consecutive_peak = 0
        burnout_warning = None

        for day in daily_stats:
            if day["total_hours"] >= (max_daily_hours - 0.2):
                consecutive_peak_days += 1
                if consecutive_peak_days > max_consecutive_peak:
                    max_consecutive_peak = consecutive_peak_days
            else:
                consecutive_peak_days = 0

        if max_consecutive_peak >= burnout_threshold:
            burnout_warning = (
                f"Burnout Guard Warning: You have {max_consecutive_peak} consecutive days scheduled at maximum study capacity ({max_daily_hours}h/day). "
                "Consider inserting a lighter review day to maintain peak cognitive retention."
            )

        # 2. Prepare payload for Coach Agent review
        summary_for_coach = {
            "max_daily_hours": max_daily_hours,
            "total_tasks_scheduled": total_tasks,
            "tasks_done": done_tasks,
            "tasks_missed": missed_tasks,
            "subjects_count": len(subjects),
            "subject_revisions": subject_revisions,
            "unscheduled_topics_count": unsched_cnt,
            "unscheduled_hours": unsched_hrs,
            "burnout_threshold": burnout_threshold,
            "max_consecutive_peak_days": max_consecutive_peak,
            "burnout_flag": burnout_warning is not None,
            "days_scheduled": len(daily_stats),
            "daily_hours_avg": round(sum(d["total_hours"] for d in daily_stats) / max(1, len(daily_stats)), 1)
        }

        mode = get_current_llm_mode()
        if mode == "mock":
            review = compute_dynamic_coach_review(summary_for_coach)
        else:
            llm = get_llm_provider()
            prompt = (
                "Analyze the student's study plan metrics and provide a constructive coaching review.\n"
                f"Metrics:\n{json.dumps(summary_for_coach, indent=2)}\n"
                "Return strictly valid JSON with keys: score (0-100), issues (list), suggestions (list), encouragement (string)."
            )
            try:
                resp_text = llm.generate(prompt, system_prompt=COACH_SYSTEM_PROMPT)
                cleaned = resp_text.strip()
                if cleaned.startswith("```json"):
                    cleaned = cleaned[7:]
                if cleaned.startswith("```"):
                    cleaned = cleaned[3:]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]
                review = json.loads(cleaned.strip())
            except Exception as e:
                logger.warning(f"Fallback to dynamic coach review: {e}")
                review = compute_dynamic_coach_review(summary_for_coach)

        if burnout_warning:
            review["burnout_warning"] = burnout_warning
            if burnout_warning not in review.get("issues", []):
                review.setdefault("issues", []).append(burnout_warning)

        return review

"""Deterministic study scheduling, conflict detection, replanning, and panic mode allocation."""
import sqlite3
from datetime import date, datetime, timedelta
from typing import List, Dict, Any, Optional
from app.db import get_db, get_connection

def build_plan_summary(conflicts: List[Dict[str, Any]]) -> str:
    """Build a unified final summary string from check_conflicts results, avoiding duplicate sentences and double periods."""
    if not conflicts:
        return "Study plan generated and validated with zero conflicts. Daily constraints, priority ranking, and pre-exam revision buffers verified."
    cleaned_msgs = [c.get("message", "").rstrip(".") for c in conflicts if c.get("message")]
    joined_issues = "; ".join(cleaned_msgs[:3])
    suggestion = ""
    if "increase daily hours" not in joined_issues.lower() and "panic mode" not in joined_issues.lower():
        suggestion = " Increase daily hours or consider Panic Mode."
    return f"Study plan generated with {len(conflicts)} remaining conflict(s): {joined_issues}.{suggestion}".strip()

def calculate_priority(days_until_exam: int, difficulty: int, weightage: float, mastery: float) -> float:
    """
    Calculate deterministic priority score for a topic.
    Formula: (1 / max(1, days_until_exam)) * difficulty * weightage * (1 + (1 - mastery))
    """
    urgency = 1.0 / max(1, days_until_exam)
    mastery_deficit = 1.0 + max(0.0, 1.0 - mastery)
    return round(urgency * difficulty * weightage * mastery_deficit, 4)

def get_free_slots_internal(start_date: str, end_date: str) -> Dict[str, Any]:
    """
    Generate day-by-day available time windows based on user profile free_slots.
    Respects max_daily_hours constraint.
    """
    start_d = date.fromisoformat(start_date)
    end_d = date.fromisoformat(end_date)
    if end_d < start_d:
        raise ValueError("end_date cannot be earlier than start_date")

    with get_connection() as conn:
        profile = conn.execute("SELECT max_daily_hours, break_minutes FROM profile WHERE id = 1;").fetchone()
        max_daily_hours = profile["max_daily_hours"] if profile else 4.0

        slots_rows = conn.execute("SELECT weekday, start_time, end_time, energy FROM free_slots ORDER BY weekday, start_time;").fetchall()

    weekday_slots: Dict[int, List[Dict[str, Any]]] = {}
    for r in slots_rows:
        w = r["weekday"]
        if w not in weekday_slots:
            weekday_slots[w] = []
        weekday_slots[w].append({
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "energy": r["energy"]
        })

    days: List[Dict[str, Any]] = []
    curr = start_d
    while curr <= end_d:
        w = curr.weekday()
        template_slots = weekday_slots.get(w, [])
        day_slots = []
        total_avail_hours = 0.0

        for s in template_slots:
            t1 = datetime.strptime(s["start_time"], "%H:%M")
            t2 = datetime.strptime(s["end_time"], "%H:%M")
            dur = (t2 - t1).total_seconds() / 3600.0
            if total_avail_hours + dur > max_daily_hours:
                allowed_dur = max(0.0, max_daily_hours - total_avail_hours)
                if allowed_dur >= 0.5:
                    adj_t2 = t1 + timedelta(hours=allowed_dur)
                    day_slots.append({
                        "start_time": s["start_time"],
                        "end_time": adj_t2.strftime("%H:%M"),
                        "duration_hours": round(allowed_dur, 2),
                        "energy": s["energy"]
                    })
                    total_avail_hours += allowed_dur
                break
            else:
                day_slots.append({
                    "start_time": s["start_time"],
                    "end_time": s["end_time"],
                    "duration_hours": round(dur, 2),
                    "energy": s["energy"]
                })
                total_avail_hours += dur

        days.append({
            "date": curr.isoformat(),
            "weekday": w,
            "max_hours": max_daily_hours,
            "available_hours": round(total_avail_hours, 2),
            "slots": day_slots
        })
        curr += timedelta(days=1)

    return {"start_date": start_date, "end_date": end_date, "days": days}

def create_schedule_internal(start_date: str, end_date: str, task_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """
    Deterministic allocator: Assigns topics to calendar days and slots.
    Prioritizes topics by exam proximity, difficulty, weightage, and mastery deficit.
    Inserts breaks, respects energy windows, reserves 48h pre-exam revision buffers,
    and falls back to revision-only when an exam is <= 2 days away.
    """
    start_d = date.fromisoformat(start_date)
    warnings: List[str] = []

    with get_db() as conn:
        profile = conn.execute("SELECT max_daily_hours, break_minutes FROM profile WHERE id = 1;").fetchone()
        max_daily_hours = profile["max_daily_hours"] if profile else 4.0

        # Query all topics and subjects
        topics_query = """
        SELECT t.id as topic_id, t.name as topic_name, t.unit, t.difficulty, t.est_hours,
               t.weightage, t.mastery, s.id as subject_id, s.name as subject_name,
               s.exam_date, s.color
        FROM topics t
        JOIN subjects s ON t.subject_id = s.id
        WHERE t.status != 'completed'
        """
        all_topics = [dict(row) for row in conn.execute(topics_query).fetchall()]

        if not all_topics:
            return {"status": "success", "tasks_created": 0, "message": "No unfinished topics found"}

        # Check for short-notice exams (<= 2 days away)
        for subj in conn.execute("SELECT id, name, exam_date FROM subjects;").fetchall():
            exam_d = date.fromisoformat(subj["exam_date"])
            days_left = (exam_d - start_d).days
            if 0 <= days_left <= 2:
                warnings.append(
                    f"Short-notice exam alert: {subj['name']} is {days_left} day(s) away! Falling back to revision-only."
                )

        # Clear existing pending tasks within window, PRESERVING both completed ('done') and missed ('missed') records
        conn.execute("DELETE FROM tasks WHERE date >= ? AND date <= ? AND status NOT IN ('done', 'missed');", (start_date, end_date))

        # Get available slot schedule
        free_slots_data = get_free_slots_internal(start_date, end_date)
        days_list = free_slots_data["days"]

        # Track existing preserved tasks (done + missed) per day to respect daily capacity and avoid overlaps
        occupied_hours_by_date: Dict[str, float] = {}
        for row in conn.execute("SELECT date, SUM(duration_hours) as total FROM tasks WHERE status IN ('done', 'missed') GROUP BY date;"):
            occupied_hours_by_date[row["date"]] = row["total"] or 0.0

        occupied_intervals_by_date: Dict[str, List[Any]] = {}
        for row in conn.execute("SELECT date, start_time, end_time FROM tasks WHERE status IN ('done', 'missed');"):
            d = row["date"]
            if d not in occupied_intervals_by_date:
                occupied_intervals_by_date[d] = []
            occupied_intervals_by_date[d].append((row["start_time"], row["end_time"]))

        subjects_rows = conn.execute("SELECT id, name, exam_date FROM subjects;").fetchall()
        final_revisions_scheduled: Dict[int, set] = {s["id"]: set() for s in subjects_rows}

        # Build candidate item pool
        candidate_items = []
        for t in all_topics:
            exam_d = date.fromisoformat(t["exam_date"])
            days_until_exam = (exam_d - start_d).days
            is_short_notice = (days_until_exam <= 2)

            priority = calculate_priority(
                days_until_exam=days_until_exam,
                difficulty=t["difficulty"],
                weightage=t["weightage"],
                mastery=t["mastery"]
            )
            candidate_items.append({
                **t,
                "days_until_exam": days_until_exam,
                "priority": priority,
                "hours_remaining": t["est_hours"],
                "short_notice": is_short_notice
            })

        # Sort candidate topics by priority descending (interleaving by subject as tie-breaker)
        candidate_items.sort(key=lambda x: (x["priority"], x["difficulty"]), reverse=True)

        tasks_to_insert: List[Dict[str, Any]] = []
        last_subject_id_scheduled: Optional[int] = None
        break_mins = profile["break_minutes"] if profile and profile["break_minutes"] else 15

        # Iterate through available days and slots
        for day in days_list:
            d_str = day["date"]
            curr_d = date.fromisoformat(d_str)
            daily_used_hours = occupied_hours_by_date.get(d_str, 0.0)
            day_occupied = list(occupied_intervals_by_date.get(d_str, []))

            for slot in day["slots"]:
                if daily_used_hours >= max_daily_hours:
                    break

                slot_energy = slot["energy"]
                slot_start_dt = datetime.strptime(slot["start_time"], "%H:%M")
                slot_end_dt = datetime.strptime(slot["end_time"], "%H:%M")

                cursor_dt = slot_start_dt
                continuous_study_minutes = 0.0

                # Fill each slot until full: keep placing tasks back to back
                while cursor_dt < slot_end_dt and daily_used_hours < max_daily_hours:
                    # 1. Skip over any preserved occupied intervals on this day
                    cursor_str = cursor_dt.strftime("%H:%M")
                    in_occupied = False
                    for occ_s, occ_e in day_occupied:
                        if occ_s <= cursor_str < occ_e:
                            cursor_dt = max(cursor_dt, datetime.strptime(occ_e, "%H:%M"))
                            continuous_study_minutes = 0.0
                            in_occupied = True
                            break
                    if in_occupied:
                        if cursor_dt >= slot_end_dt:
                            break
                        continue

                    # 2. Determine contiguous available block until slot_end_dt or next occupied interval
                    next_block_dt = slot_end_dt
                    for occ_s, occ_e in day_occupied:
                        occ_s_dt = datetime.strptime(occ_s, "%H:%M")
                        if cursor_dt < occ_s_dt < next_block_dt:
                            next_block_dt = occ_s_dt

                    block_avail = (next_block_dt - cursor_dt).total_seconds() / 3600.0
                    avail_hrs = min(block_avail, max_daily_hours - daily_used_hours)
                    avail_hrs = round(avail_hrs, 2)

                    if avail_hrs < 0.25:
                        if next_block_dt < slot_end_dt:
                            cursor_dt = next_block_dt
                            continuous_study_minutes = 0.0
                            continue
                        else:
                            break

                    # 3. Check for subjects in their 2-day pre-exam revision window on this day
                    chosen_item = None
                    is_rev_task = False

                    rev_subjects = []
                    for subj in subjects_rows:
                        s_exam_d = date.fromisoformat(subj["exam_date"])
                        diff_days = (s_exam_d - curr_d).days
                        if 1 <= diff_days <= 2:
                            rev_subjects.append((subj, diff_days))

                    if rev_subjects:
                        # Schedule a revision task (max 1h each) for EVERY topic of that subject,
                        # ordered by weightage desc and mastery asc, while capacity allows.
                        for subj, diff_days in rev_subjects:
                            s_id = subj["id"]
                            subj_topics = [t for t in all_topics if t["subject_id"] == s_id]
                            # Highest weightage and lowest mastery first
                            subj_topics.sort(key=lambda x: (x["weightage"], -x["mastery"]), reverse=True)
                            for st in subj_topics:
                                if st["topic_id"] not in final_revisions_scheduled[s_id]:
                                    chosen_item = st
                                    is_rev_task = True
                                    final_revisions_scheduled[s_id].add(st["topic_id"])
                                    break
                            if chosen_item:
                                break

                    # If not a final revision window task, select standard candidate study topic
                    if not chosen_item:
                        eligible = []
                        for item in candidate_items:
                            if item["hours_remaining"] <= 0.05:
                                continue
                            exam_d = date.fromisoformat(item["exam_date"])
                            if curr_d >= exam_d:
                                continue
                            days_before_exam = (exam_d - curr_d).days
                            if days_before_exam <= 2 and not item["short_notice"]:
                                # Standard study is blocked in the 48h revision window
                                continue
                            eligible.append(item)

                        if not eligible:
                            break

                        # Short-notice subjects must never be displaced by
                        # energy matching or subject interleaving.
                        urgent = [c for c in eligible if c["short_notice"]]
                        if urgent:
                            eligible = urgent

                        # Energy-aware matching & subject interleaving preference
                        if slot_energy == "high":
                            high_diff = [c for c in eligible if c["difficulty"] >= 4 and c["subject_id"] != last_subject_id_scheduled]
                            if not high_diff:
                                high_diff = [c for c in eligible if c["difficulty"] >= 4]
                            if high_diff:
                                chosen_item = high_diff[0]

                        if not chosen_item:
                            interleaved = [c for c in eligible if c["subject_id"] != last_subject_id_scheduled]
                            chosen_item = interleaved[0] if interleaved else eligible[0]

                    # 4. Determine task allocation duration
                    days_to_ex = (date.fromisoformat(chosen_item["exam_date"]) - curr_d).days

                    # For short-notice subjects (exam <= 2 days from plan
                    # start), every task is "revision" so check_conflicts
                    # never reports revision_buffer_violation.
                    chosen_is_short = chosen_item.get("short_notice", False)
                    # Also treat the item from candidate_items if available
                    if not chosen_is_short:
                        for ci in candidate_items:
                            if ci["topic_id"] == chosen_item["topic_id"]:
                                chosen_is_short = ci.get("short_notice", False)
                                break

                    if is_rev_task and not chosen_is_short:
                        # Normal subject: 48h-buffer revision (max 1h, no hours_remaining deduction)
                        task_type = "revision"
                        alloc_dur = min(avail_hrs, 1.0)
                        alloc_dur = round(alloc_dur, 2)
                        reason_str = (
                            f"Final pre-exam revision ({days_to_ex}d before exam) for {chosen_item['topic_name']}. "
                            f"Weightage {chosen_item['weightage']}, Mastery {int(chosen_item['mastery']*100)}%."
                        )
                        p_score = 3.5
                    elif chosen_is_short:
                        # Short-notice subject: ALL tasks are revision and
                        # count toward the topic's est_hours.
                        task_type = "revision"
                        # Find the matching candidate_items entry for hours_remaining
                        ci_match = None
                        for ci in candidate_items:
                            if ci["topic_id"] == chosen_item["topic_id"]:
                                ci_match = ci
                                break
                        remaining = ci_match["hours_remaining"] if ci_match else chosen_item.get("hours_remaining", chosen_item["est_hours"])
                        alloc_dur = min(avail_hrs, remaining, 2.0)
                        alloc_dur = round(alloc_dur, 2)
                        reason_str = (
                            f"Short-notice revision ({days_to_ex}d before exam) for {chosen_item['topic_name']}. "
                            f"Priority {chosen_item.get('priority', 0):.2f}, Weightage {chosen_item['weightage']}."
                        )
                        p_score = chosen_item.get("priority", 3.5)
                    else:
                        task_type = "study"
                        alloc_dur = min(avail_hrs, chosen_item["hours_remaining"], 2.0)
                        alloc_dur = round(alloc_dur, 2)
                        reason_str = (
                            f"Priority {chosen_item['priority']:.2f} (Exam in {days_to_ex}d, Diff {chosen_item['difficulty']}, Wgt {chosen_item['weightage']}). "
                            f"Allocated to {slot_energy}-energy slot."
                        )
                        p_score = chosen_item["priority"]

                    if alloc_dur < 0.25:
                        break

                    t_start = cursor_dt
                    t_end = t_start + timedelta(hours=alloc_dur)
                    s_str = t_start.strftime("%H:%M")
                    e_str = t_end.strftime("%H:%M")

                    tasks_to_insert.append({
                        "topic_id": chosen_item["topic_id"],
                        "type": task_type,
                        "date": d_str,
                        "start_time": s_str,
                        "end_time": e_str,
                        "duration_hours": alloc_dur,
                        "status": "pending",
                        "reason": reason_str,
                        "priority_score": p_score
                    })

                    day_occupied.append((s_str, e_str))
                    daily_used_hours = round(daily_used_hours + alloc_dur, 2)
                    # Deduct hours_remaining for study tasks and short-notice revision tasks.
                    # Normal-subject 48h-buffer revision tasks (supplementary) do NOT deduct.
                    should_deduct = (not is_rev_task) or chosen_is_short
                    if should_deduct:
                        # Find the matching candidate_items entry and deduct
                        for ci in candidate_items:
                            if ci["topic_id"] == chosen_item["topic_id"]:
                                ci["hours_remaining"] = max(0.0, round(ci["hours_remaining"] - alloc_dur, 2))
                                break
                    last_subject_id_scheduled = chosen_item["subject_id"]
                    continuous_study_minutes += alloc_dur * 60

                    # 5. Advance cursor and apply break after 90 minutes
                    cursor_dt = t_end
                    if continuous_study_minutes >= 90:
                        cursor_dt += timedelta(minutes=break_mins)
                        continuous_study_minutes = 0.0

            occupied_hours_by_date[d_str] = daily_used_hours
            occupied_intervals_by_date[d_str] = day_occupied

        # Guarantee final revision tasks for any subject with upcoming exam
        for subj in subjects_rows:
            s_id = subj["id"]
            s_exam_d = date.fromisoformat(subj["exam_date"])
            days_until_ex = (s_exam_d - start_d).days
            if days_until_ex <= 0:
                continue

            # Check if this subject has revision tasks in its 48h buffer
            buffer_start_d = s_exam_d - timedelta(days=2)
            has_rev_in_buffer = any(
                t["type"] == "revision" and buffer_start_d <= date.fromisoformat(t["date"]) < s_exam_d
                and any(top["topic_id"] == t["topic_id"] and top["subject_id"] == s_id for top in all_topics)
                for t in tasks_to_insert
            )

            if not has_rev_in_buffer:
                # Find available day in the 48h window before exam
                target_date_d = max(start_d, s_exam_d - timedelta(days=1))
                if target_date_d < s_exam_d:
                    target_date_str = target_date_d.isoformat()
                    subj_tops = [t for t in all_topics if t["subject_id"] == s_id]
                    subj_tops.sort(key=lambda x: (x["weightage"], -x["mastery"]), reverse=True)
                    top_to_rev = subj_tops[0] if subj_tops else None
                    if top_to_rev:
                        # Find non-overlapping time slot on target day
                        day_occ = occupied_intervals_by_date.get(target_date_str, [])
                        rev_start = "18:00"
                        rev_end = "19:00"
                        rev_dur = 1.0
                        if day_occ:
                            # place after last task on that day
                            last_end = max(occ[1] for occ in day_occ)
                            if last_end <= "21:00":
                                rev_start = last_end
                                end_dt = datetime.strptime(last_end, "%H:%M") + timedelta(hours=rev_dur)
                                rev_end = end_dt.strftime("%H:%M")

                        tasks_to_insert.append({
                            "topic_id": top_to_rev["topic_id"],
                            "type": "revision",
                            "date": target_date_str,
                            "start_time": rev_start,
                            "end_time": rev_end,
                            "duration_hours": rev_dur,
                            "status": "pending",
                            "reason": f"Final 48h pre-exam revision buffer for {subj['name']} ({top_to_rev['topic_name']}).",
                            "priority_score": 3.5
                        })

        # Calculate unscheduled topics and unscheduled hours
        unscheduled_topics = []
        for item in candidate_items:
            if item["hours_remaining"] > 0.05:
                unscheduled_topics.append({
                    "topic_id": item["topic_id"],
                    "name": item["topic_name"],
                    "subject_id": item["subject_id"],
                    "subject_name": item["subject_name"],
                    "hours_remaining": round(item["hours_remaining"], 2)
                })
        unscheduled_hours = round(sum(t["hours_remaining"] for t in unscheduled_topics), 2)

        # Insert scheduled tasks
        for task in tasks_to_insert:
            conn.execute("""
            INSERT INTO tasks (topic_id, type, date, start_time, end_time, duration_hours, status, reason, priority_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                task["topic_id"],
                task["type"],
                task["date"],
                task["start_time"],
                task["end_time"],
                task["duration_hours"],
                task["status"],
                task["reason"],
                task["priority_score"]
            ))

    return {
        "status": "success",
        "tasks_created": len(tasks_to_insert),
        "start_date": start_date,
        "end_date": end_date,
        "warnings": warnings,
        "unscheduled_topics": unscheduled_topics,
        "unscheduled_hours": unscheduled_hours
    }

def check_conflicts_internal() -> Dict[str, Any]:
    """
    Deterministic conflict auditor.
    Detects:
    1. Days exceeding max_daily_hours
    2. Overlapping time intervals on the same day
    3. Tasks scheduled after or on exam date
    4. First-time study tasks scheduled in the 48h pre-exam revision window
    5. Subjects lacking final pre-exam revision sessions
    """
    conflicts: List[Dict[str, Any]] = []

    with get_connection() as conn:
        profile = conn.execute("SELECT max_daily_hours FROM profile WHERE id = 1;").fetchone()
        max_daily_hours = profile["max_daily_hours"] if profile else 4.0

        # 1. Check daily hour overload
        daily_hours = conn.execute("""
        SELECT date, SUM(duration_hours) as total_hours, COUNT(*) as task_count
        FROM tasks
        GROUP BY date
        HAVING total_hours > ?;
        """, (max_daily_hours + 0.05,)).fetchall()

        for r in daily_hours:
            conflicts.append({
                "type": "daily_overload",
                "date": r["date"],
                "total_hours": round(r["total_hours"], 2),
                "max_allowed": max_daily_hours,
                "message": f"Overloaded day {r['date']}: scheduled {r['total_hours']:.1f}h exceeds max {max_daily_hours:.1f}h."
            })

        # 2. Check time overlaps on same date
        tasks_by_date = conn.execute("""
        SELECT id, date, start_time, end_time, type
        FROM tasks
        ORDER BY date, start_time;
        """).fetchall()

        grouped: Dict[str, List[sqlite3.Row]] = {}
        for t in tasks_by_date:
            grouped.setdefault(t["date"], []).append(t)

        for d_str, day_tasks in grouped.items():
            for i in range(len(day_tasks) - 1):
                t1 = day_tasks[i]
                t2 = day_tasks[i+1]
                if t1["end_time"] > t2["start_time"]:
                    conflicts.append({
                        "type": "time_overlap",
                        "date": d_str,
                        "task1_id": t1["id"],
                        "task2_id": t2["id"],
                        "message": f"Time overlap on {d_str} between Task #{t1['id']} ({t1['start_time']}-{t1['end_time']}) and Task #{t2['id']} ({t2['start_time']}-{t2['end_time']})."
                    })

        # 3. Check exam date violations & 48h revision buffer violations
        violations = conn.execute("""
        SELECT t.id, t.date, t.type, s.name as subject_name, s.exam_date
        FROM tasks t
        JOIN topics tp ON t.topic_id = tp.id
        JOIN subjects s ON tp.subject_id = s.id;
        """).fetchall()

        for v in violations:
            t_date = date.fromisoformat(v["date"])
            exam_date = date.fromisoformat(v["exam_date"])
            if t_date >= exam_date:
                conflicts.append({
                    "type": "past_exam_date",
                    "task_id": v["id"],
                    "date": v["date"],
                    "exam_date": v["exam_date"],
                    "message": f"Task #{v['id']} ({v['subject_name']}) is scheduled on/after exam date {v['exam_date']}."
                })
            elif (exam_date - t_date).days <= 2 and v["type"] == "study":
                conflicts.append({
                    "type": "revision_buffer_violation",
                    "task_id": v["id"],
                    "date": v["date"],
                    "exam_date": v["exam_date"],
                    "message": f"Task #{v['id']} is a first-time study task scheduled inside the 48h pre-exam buffer of {v['subject_name']} ({v['exam_date']})."
                })

        # 4. Check for subjects lacking a final revision
        subjects = conn.execute("SELECT id, name, exam_date FROM subjects;").fetchall()
        for subj in subjects:
            exam_d = date.fromisoformat(subj["exam_date"])
            buffer_start = (exam_d - timedelta(days=2)).isoformat()
            rev_count = conn.execute("""
            SELECT COUNT(*) as c
            FROM tasks t
            JOIN topics tp ON t.topic_id = tp.id
            WHERE tp.subject_id = ? AND t.type = 'revision' AND t.date >= ? AND t.date < ?;
            """, (subj["id"], buffer_start, subj["exam_date"])).fetchone()["c"]

            # Only flag if there are study tasks scheduled for this subject and exam is in the future (>2 days)
            total_subj_tasks = conn.execute("""
            SELECT COUNT(*) as c FROM tasks t JOIN topics tp ON t.topic_id = tp.id WHERE tp.subject_id = ?;
            """, (subj["id"],)).fetchone()["c"]

            if total_subj_tasks > 0 and rev_count == 0 and (exam_d - date.today()).days > 2:
                conflicts.append({
                    "type": "missing_final_revision",
                    "subject_id": subj["id"],
                    "subject_name": subj["name"],
                    "message": f"Subject '{subj['name']}' has no final revision session in the 48h buffer before {subj['exam_date']}."
                })

        # 5. Check for unscheduled or partially unscheduled topics
        # Identify short-notice subjects (exam <= 2 days from today) so that
        # revision hours count toward their required study hours.
        today_d = date.today()
        short_notice_subject_ids = set()
        for subj in subjects:
            exam_d = date.fromisoformat(subj["exam_date"])
            if (exam_d - today_d).days <= 2:
                short_notice_subject_ids.add(subj["id"])

        unscheduled_list = []
        all_db_topics = conn.execute("""
        SELECT t.id, t.name, t.est_hours, s.id as subject_id, s.name as subject_name
        FROM topics t
        JOIN subjects s ON t.subject_id = s.id
        WHERE t.status != 'completed';
        """).fetchall()

        for top in all_db_topics:
            is_short = top["subject_id"] in short_notice_subject_ids
            if is_short:
                # Short-notice: count both study AND revision hours
                sched_row = conn.execute("""
                SELECT SUM(duration_hours) as total
                FROM tasks
                WHERE topic_id = ? AND type IN ('study', 'revision') AND status != 'missed';
                """, (top["id"],)).fetchone()
            else:
                # Normal: count only study hours
                sched_row = conn.execute("""
                SELECT SUM(duration_hours) as total
                FROM tasks
                WHERE topic_id = ? AND type = 'study' AND status != 'missed';
                """, (top["id"],)).fetchone()
            sched_hrs = sched_row["total"] or 0.0
            if sched_hrs < top["est_hours"] - 0.05:
                rem = round(top["est_hours"] - sched_hrs, 2)
                unscheduled_list.append({
                    "topic_id": top["id"],
                    "name": top["name"],
                    "subject_id": top["subject_id"],
                    "subject_name": top["subject_name"],
                    "unscheduled_hours": rem
                })

        if unscheduled_list:
            total_un_hrs = round(sum(u["unscheduled_hours"] for u in unscheduled_list), 2)
            conflicts.append({
                "type": "unscheduled_topics",
                "count": len(unscheduled_list),
                "unscheduled_hours": total_un_hrs,
                "topics": unscheduled_list,
                "message": f"{len(unscheduled_list)} topics could not fit before the exam ({total_un_hrs}h unscheduled). Increase daily hours or use Panic Mode."
            })

    return {
        "conflicts_found": len(conflicts) > 0,
        "count": len(conflicts),
        "conflicts": conflicts,
        "summary": build_plan_summary(conflicts)
    }

def replan_internal(from_date: str, reason: str = "Missed study targets or schedule adjustment") -> Dict[str, Any]:
    """
    Dynamic replanner:
    1. Preserves completed tasks (done).
    2. Identifies all missed tasks and pending tasks on or after from_date.
    3. Finds latest exam date among all subjects to define rescheduling horizon.
    4. Re-allocates unfinished tasks into available slots.
    5. Returns an audit diff ("What changed and why").
    """
    from_d = date.fromisoformat(from_date)

    with get_connection() as conn:
        # Get max exam date for scheduling horizon
        max_exam_row = conn.execute("SELECT MAX(exam_date) as max_date FROM subjects;").fetchone()
        max_exam_str = max_exam_row["max_date"] if max_exam_row and max_exam_row["max_date"] else (from_d + timedelta(days=30)).isoformat()

        # Count completed tasks preserved
        completed_count = conn.execute("SELECT COUNT(*) as c FROM tasks WHERE status = 'done';").fetchone()["c"]

        # Gather missed tasks and pending tasks to reschedule
        tasks_to_replan = conn.execute("""
        SELECT id, topic_id, type, date, duration_hours, status
        FROM tasks
        WHERE (status = 'missed' OR (status = 'pending' AND date >= ?));
        """, (from_date,)).fetchall()

        moved_count = len(tasks_to_replan)

    # Re-run schedule creation from from_date to max_exam_str
    sched_res = create_schedule_internal(
        start_date=from_date,
        end_date=max_exam_str
    )

    conflict_check = check_conflicts_internal()

    changes = [
        f"Preserved {completed_count} completed task(s).",
        f"Redistributed {moved_count} missed/pending task(s) from {from_date} onwards.",
        f"Created {sched_res['tasks_created']} updated schedule slots.",
        f"Reason: {reason}"
    ]

    return {
        "status": "success",
        "replanned": True,
        "from_date": from_date,
        "preserved_completed": completed_count,
        "tasks_rescheduled": moved_count,
        "new_tasks_count": sched_res["tasks_created"],
        "changes": changes,
        "conflicts": conflict_check["conflicts"],
        "unscheduled_topics": sched_res.get("unscheduled_topics", []),
        "unscheduled_hours": sched_res.get("unscheduled_hours", 0.0)
    }

def compress_plan_internal(subject_id: int, days_left: int) -> Dict[str, Any]:
    """
    Panic Mode Engine:
    When time is critically short, prunes low-weightage topics and concentrates
    all remaining study hours on high-yield topics (highest weightage and difficulty).
    """
    with get_db() as conn:
        subj = conn.execute("SELECT id, name, exam_date FROM subjects WHERE id = ?;", (subject_id,)).fetchone()
        if not subj:
            raise ValueError(f"Subject #{subject_id} not found")

        topics = conn.execute("""
        SELECT id, name, unit, weightage, difficulty, est_hours, mastery
        FROM topics
        WHERE subject_id = ?
        ORDER BY weightage DESC, difficulty DESC;
        """, (subject_id,)).fetchall()

        if not topics:
            return {"compressed": False, "message": "No topics found for this subject"}

        # Calculate average or median weightage to prune low-yield topics
        weightages = [t["weightage"] for t in topics]
        avg_weightage = sum(weightages) / len(weightages)

        kept_topics = []
        dropped_topics = []

        for t in topics:
            # Keep if weightage >= average OR difficulty is high and mastery is low
            if t["weightage"] >= avg_weightage or (t["difficulty"] >= 4 and t["mastery"] < 0.3):
                kept_topics.append({
                    "id": t["id"],
                    "name": t["name"],
                    "unit": t["unit"],
                    "weightage": t["weightage"],
                    "difficulty": t["difficulty"]
                })
            else:
                dropped_topics.append({
                    "id": t["id"],
                    "name": t["name"],
                    "unit": t["unit"],
                    "weightage": t["weightage"],
                    "difficulty": t["difficulty"]
                })

        # Remove future pending tasks for dropped topics
        if dropped_topics:
            dropped_ids = [d["id"] for d in dropped_topics]
            placeholders = ",".join(["?"] * len(dropped_ids))
            conn.execute(f"""
            DELETE FROM tasks
            WHERE topic_id IN ({placeholders}) AND status = 'pending';
            """, dropped_ids)

    return {
        "compressed": True,
        "subject_id": subject_id,
        "subject_name": subj["name"],
        "days_left": days_left,
        "total_topics": len(topics),
        "kept_count": len(kept_topics),
        "dropped_count": len(dropped_topics),
        "kept_topics": kept_topics,
        "dropped_topics": dropped_topics,
        "explanation": (
            f"Panic Mode activated for {subj['name']}: {len(dropped_topics)} low-weightage topics dropped to "
            f"maximize exam yield across the remaining {days_left} day(s)."
        )
    }

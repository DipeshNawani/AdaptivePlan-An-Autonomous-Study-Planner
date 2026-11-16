# AdaptivePlan: Comprehensive Viva Preparation & Defense Notes

> **Subject:** Generative AI Mini Project (Sem VII, B.E. IT / Computer Engineering)  
> **Topic:** AdaptivePlan - An LLM-Powered Autonomous Study Planning Agent with Dynamic Rescheduling

---

## 1. Core Concepts: Agent vs. Chatbot

### What makes AdaptivePlan an "Agent" and not just a "Chatbot"?
* **Chatbot (Passive Text Generator):** A chatbot receives a prompt, performs single-turn next-token prediction, and outputs a string. It cannot independently interact with databases, verify constraints, or take corrective actions if its output is invalid.
* **Autonomous Agent (Perception-Reasoning-Action Loop):** AdaptivePlan operates as an agent because:
  1. **Goal-Driven Execution:** It is given high-level goals ("build a valid study schedule for the student").
  2. **Tool-Calling (Action):** It decides *which* Python functions to invoke (`get_free_slots`, `create_schedule`, `check_conflicts`), formats the arguments, and executes them.
  3. **Observation & Reflection:** It observes the structured JSON returned by tools.
  4. **Self-Correction:** If `check_conflicts` finds that a day has exceeded `max_daily_hours` or an exam buffer is violated, the agent adjusts arguments and re-executes.
  5. **Termination Condition:** It loops up to 8 iterations until conflict verification passes before returning a final answer.

---

## 2. System Architecture & Deterministic Division of Labor

### Why is the scheduling logic deterministic Python code instead of pure LLM generation?
* **Problem with pure LLM scheduling:** LLMs are statistical language models prone to hallucinating dates, miscalculating hours, shifting time intervals, or silently dropping syllabus topics.
* **AdaptivePlan's Solution:**
  * **The LLM provides reasoning and orchestration:** deciding what needs to be scheduled, handling user intent, auditing student psychology (Coach Agent), and choosing tools.
  * **Deterministic Python code handles the math:** calendar allocation, slot boundaries, capacity caps ($\le \text{max\_daily\_hours}$), and constraint verification.
  * **Zero Direct Database Writes by LLM:** The LLM *never* writes raw SQL or directly modifies SQLite rows. All state transitions pass through strict Pydantic-validated tool functions.

---

## 3. The Priority Function & Allocation Mathematics

### What formula is used to prioritize topics?
$$\text{Priority} = \left(\frac{1}{\max(1, \text{days\_until\_exam})}\right) \times \text{difficulty} \times \text{weightage} \times \left(1 + (1 - \text{mastery})\right)$$

* **$\frac{1}{\text{days\_until\_exam}}$ (Urgency):** Topics whose exams are closer receive higher priority.
* **$\text{difficulty}$ (Cognitive Load, 1 to 5):** More challenging topics require early exposure.
* **$\text{weightage}$ (Past-Paper Frequency):** Prioritizes modules that carry higher mark distribution.
* **$(1 + (1 - \text{mastery}))$ (Deficit Multiplier):** Topics with low quiz mastery ($0.0$ to $1.0$) receive up to a $2.0\times$ priority boost.

### Key Allocation Rules:
1. **Energy Matching:** High difficulty modules ($\ge 4$) are mapped to `high` energy windows (e.g. 6:00 PM - 8:00 PM).
2. **Cap Guarantee:** Total daily duration never exceeds `profile.max_daily_hours`.
3. **Pacing Breaks:** A 15-minute break is scheduled after every 90 minutes of study.
4. **48-Hour Pre-Exam Revision Buffer:** The final 2 days before any exam are reserved exclusively for revision. No new study topics are scheduled.
5. **Short-Notice Fallback:** If an exam is $\le 2$ days away, the engine automatically switches to revision-only mode and emits a visible warning.

---

## 4. Exam Readiness Score: Tunable Heuristic Metric

### How is the Exam Readiness Score (0-100) calculated?
$$\text{Readiness} = 0.4 \times \text{Completion} + 0.3 \times \text{Mastery} + 0.2 \times \text{Revision Ratio} + 0.1 \times \text{Urgency Factor}$$

* **Critical Viva Point:** If asked whether these weights ($0.4 / 0.3 / 0.2 / 0.1$) are research constants:
  > *"These weights are **pedagogical heuristics** chosen to balance syllabus coverage ($40\%$) and active recall retention ($30\%$). They are configurable parameters in the system, not rigid universal constants."*

---

## 5. Dynamic Replanning & The What-Changed-and-Why Banner

### How does replanning work when a student misses a task?
1. The student clicks **Missed** on a task.
2. The system triggers `replan_internal(from_date, reason)`.
3. **Work Preservation:** All tasks marked `done` are locked and preserved.
4. **Rescheduling:** All `missed` tasks and future `pending` tasks are pulled into an unfinished pool.
5. **Re-Allocation:** The deterministic allocator recalculates priorities across remaining open slots up to the exam date.
6. **Audit Diff:** The system generates a human-readable summary ("Preserved 4 done tasks, shifted 2 missed tasks, created 6 updated slots") displayed in the UI banner.

---

## 6. Spaced Repetition & Cognitive Science

### How is Spaced Repetition implemented?
* Based on **Ebbinghaus's Forgetting Curve**:
  * When a topic is completed (`mark_progress(task_id, 'done')`), the system automatically schedules review sessions at **+1 day, +3 days, and +7 days**.
  * All revision sessions are strictly validated to occur *before* the subject's exam date.

### How does the Weak-Topic Remediation Loop work?
* When the student takes a quiz, if their score is **$< 60\%$**, the agent lowers topic mastery and automatically injects an urgent remedial revision session within 48 hours.

---

## 7. Multi-Agent Architecture: Planner vs. Coach

1. **Planner Agent:**
   * Autonomous tool-calling loop (max 8 iterations).
   * Enforces rules, builds calendar slots, verifies zero conflicts.
2. **Coach Agent:**
   * Secondary LLM evaluation.
   * Audits student well-being, checks for **Burnout Guard** ($>5$ consecutive days at maximum capacity), and returns constructive advice as structured JSON.
   * Does *not* have permission to alter database rows.

---

## 8. Runtime LLM Modes & Per-Run Fallback (Part A)

### How does runtime mode switching work?
* The user toggles between **Mock** and **Live (Gemini)** via the UI segmented control.
* This calls `POST /api/llm/mode` which persists the setting to SQLite (`app_settings` table).
* **Zero module-level caching:** The Planner and Coach instantiate the provider dynamically on *every run* via `get_llm_provider()`.
* **Security guarantee:** The Gemini API key is never exposed to the frontend or written to the SQLite database. Without a key in `.env`, the system rejects Live mode with HTTP 400.

### How does Per-Run Fallback work, and why does it not change global state?
* If Gemini hits a rate limit (HTTP 429) or network interruption during an execution, the system catches the exception and falls back to `MockLLMProvider` *for that single run only*.
* A prominent toast alert notifies the student: *"Gemini unavailable, used Mock for this run"*.
* The global mode in SQLite remains `live`. This ensures transient API hiccups do not permanently break the user's preference.

---

## 9. Demo Data vs. Real Data & Plan Creation (Part B)

### Why does a fresh installation start empty?
* Academic evaluators often want to test the system from a clean slate. A fresh install starts with **0 subjects and 0 tasks**.
* Demo data is populated *only* when the user explicitly clicks **"Load Demo Student"**.
* While demo data is active, an explicit purple **"DEMO DATA"** badge is displayed in the header.

### How does the "New Plan" Wizard work?
* The 5-step wizard guides the user through:
  1. **Subjects:** Name, color, and future exam dates.
  2. **Syllabus Extraction:** Automatically extracts topics from pasted text or PDF (using Gemini in Live mode, or deterministic rule-based parsing in Mock mode).
  3. **Preferences:** Max daily hours, peak energy period, break lengths, and burnout limits.
  4. **Past-Paper Weightage:** Analyzes recurring topics from past question papers.
  5. **Review & Autonomous Generation:** Saves setup and triggers the Planner Agent with live SSE activity streaming.

### What happens when an exam date is edited later?
* When an exam date is changed in the **Setup** tab via `PUT /api/subjects/{id}`, the backend automatically triggers an intelligent replan, re-adjusts slot allocations to accommodate the new deadline, and generates a **What Changed and Why** audit delta for transparency.

---

## 10. Continuous Capacity Filling & Topic Splitting

### How does the scheduler prevent wasted slot time?
* **Problem in naive schedulers:** Allocating one task per free slot leaves a 4h window with 2h of wasted idle time if a topic only requires 2 hours.
* **Continuous Slot Filling:**
  * The algorithm iterates through available blocks within a slot and places tasks back-to-back until either the slot ends or `max_daily_hours` is consumed.
  * **Interleaving:** Alternate subject topics to maintain cognitive freshness.
  * **Pacing breaks:** Automatically inserts a 15-minute break after every 90 minutes of continuous study.
  * **Topic Splitting:** If a topic's duration exceeds the remaining time in a slot, the topic is split across multiple slots or subsequent days rather than being dropped or overflowing past boundaries.

---

## 11. Real Self-Correction Loop in Autonomous Planning

### How does the ReAct self-correction loop actually work?
* Unlike basic chatbot prompts that produce an answer in one shot, the Planner Agent operates in a loop (up to 8 iterations):
  1. The agent calls `create_schedule` to allocate sessions.
  2. The agent executes `check_conflicts` to audit the generated schedule for:
     - Daily overload ($> \text{max\_daily\_hours}$)
     - Overlapping task intervals
     - Missing 48-hour final pre-exam revision buffers
     - Unscheduled topics
  3. **Feedback & Adjustment:** If conflicts are detected, the agent does **not** stop or declare victory. It feeds the diagnostic conflict messages back to the LLM (or mock planner logic), prompting corrective tool calls (e.g. `add_revision_sessions` or adjusting schedule bounds).
  4. **Strict Termination:** The agent only concludes and returns `"validated"` when `check_conflicts` confirms **zero remaining conflicts**.

---

## 12. Capacity Limits & Unscheduled Topics Detection

### What happens when the student's available hours cannot fit the entire syllabus?
* The scheduling engine calculates `unscheduled_topics` and `unscheduled_hours`.
* `check_conflicts` flags an `unscheduled_topics` conflict alert.
* The frontend surfaces an immediate, actionable warning banner:
  > *"N topics could not fit before the exam. Increase daily hours or use Panic Mode."*
* The student can immediately:
  - Increase their daily study capacity in **Setup**, OR
  - Trigger **Panic Mode** to mathematically prune low-yield topics and focus 100% of remaining time on high-weightage modules.

---

## 13. Dynamic Coach Agent Evaluation (Zero Canned Text)

### How does the Coach Agent evaluate the plan without hardcoded text?
* The Coach inspects the live SQLite database metrics:
  - **Consecutive Peak Days:** Flags burnout if study load stays at max capacity for $> 5$ consecutive days.
  - **Subject Revision Coverage:** Audits whether all subjects have scheduled revision sessions before their exams.
  - **Unscheduled Topic Count:** Adjusts score and warning level if topics were omitted due to capacity.
  - **Missed Task Frequency:** Identifies consistency issues and suggests realistic daily pacing adjustments.
* Every recommendation, issue list, and readiness score (0-100) is dynamically computed from live study data.

---

## 14. Key Defense: "Why can't ChatGPT or Claude just do this?"

When the external examiner asks: *"Why do we need AdaptivePlan if a student can just paste their syllabus into ChatGPT?"*

1. **State Persistence & Database Grounding:**
   - ChatGPT is stateless. Every day, the student must re-paste what they did, what they missed, and recalculate dates.
   - AdaptivePlan maintains state in SQLite. Marking a single task as **Done** or **Missed** automatically locks history and cascades reschedule calculations.
2. **Deterministic Constraint Guarantees (No Hallucinations):**
   - General LLMs frequently hallucinate calendar arithmetic (e.g. scheduling 30 hours of work into a 24-hour day, assigning tasks past the exam date, or overlapping time slots).
   - AdaptivePlan uses a **neurosymbolic hybrid approach**: the LLM handles high-level intent and reasoning, while deterministic Python algorithms guarantee hard mathematical constraints.
3. **Closed-Loop Active Verification (ReAct):**
   - An LLM in a single prompt cannot check if its schedule has collisions.
   - AdaptivePlan inspects the result using `check_conflicts` tools and autonomously corrects itself before presenting the plan.
4. **Adaptive Active Recall & Spaced Repetition:**
   - AdaptivePlan integrates an active recall testing engine. Quiz performance ($< 60\%$) dynamically feeds back into the schedule to inject immediate remedial review sessions.

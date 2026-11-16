"""Deterministic Mock LLM Provider for offline demonstration, testing, and quota resilience."""
import json
import re
from datetime import date, timedelta
from typing import List, Dict, Any, Optional
from app.llm.provider import LLMProvider, LLMResponse, ToolCallRequest

class MockLLMProvider(LLMProvider):
    """
    Mock LLM provider that simulates autonomous function calling and structured reasoning.
    Ensures viva demonstrations never fail due to API outages or missing keys.
    """

    def __init__(self, is_fallback: bool = False, fallback_reason: Optional[str] = None):
        self.is_fallback = is_fallback
        self.fallback_reason = fallback_reason

    def generate(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        prompt_lower = prompt.lower()

        # Coach Agent review prompt
        if "coach" in prompt_lower or (system_prompt and "coach" in system_prompt.lower()):
            from app.agents.coach import compute_dynamic_coach_review
            metrics = {}
            if "metrics:\n" in prompt_lower:
                try:
                    idx = prompt_lower.index("metrics:\n") + len("metrics:\n")
                    end_idx = prompt.find("\nReturn", idx)
                    json_str = prompt[idx:end_idx].strip() if end_idx != -1 else prompt[idx:].strip()
                    metrics = json.loads(json_str)
                except Exception:
                    pass
            return json.dumps(compute_dynamic_coach_review(metrics), indent=2)

        # Quiz generation prompt
        if any(k in prompt_lower for k in ("quiz", "mcq", "multiple-choice", "questions", "question")):
            return json.dumps({
                "questions": [
                    {
                        "question": "Which search algorithm guarantees finding the optimal path if the heuristic is admissible?",
                        "options": ["Depth-First Search (DFS)", "Breadth-First Search (BFS)", "A* Search", "Hill Climbing"],
                        "answer": "A* Search",
                        "explanation": "A* search is both complete and optimal when using an admissible (never overestimating) heuristic."
                    },
                    {
                        "question": "What is the primary role of a Hypervisor in virtualization?",
                        "options": ["Manage databases", "Create and run virtual machines", "Compile source code", "Route internet traffic"],
                        "answer": "Create and run virtual machines",
                        "explanation": "A hypervisor (or VMM) creates and runs virtual machines by abstracting host hardware."
                    },
                    {
                        "question": "Which cryptographic principle ensures that a sender cannot deny having sent a message?",
                        "options": ["Confidentiality", "Non-repudiation", "Availability", "Integrity"],
                        "answer": "Non-repudiation",
                        "explanation": "Non-repudiation, often provided via digital signatures, prevents the author from denying message authenticity."
                    }
                ]
            }, indent=2)

        # Syllabus extraction prompt
        if "syllabus" in prompt_lower or "extract units" in prompt_lower or "extract subjects" in prompt_lower:
            from app.tools.syllabus import rule_based_syllabus_parser
            raw_content = ""
            if "content:\n" in prompt_lower:
                idx = prompt_lower.index("content:\n") + len("content:\n")
                raw_content = prompt[idx:].strip()
            elif "content:" in prompt_lower:
                idx = prompt_lower.index("content:") + len("content:")
                raw_content = prompt[idx:].strip()

            if raw_content:
                course_match = re.search(r'for (?:the course|the subject)\s*"([^"]+)"', prompt, re.IGNORECASE)
                subj_name = course_match.group(1) if course_match else None
                return json.dumps(rule_based_syllabus_parser(raw_content, subject_name=subj_name), indent=2)

            return json.dumps({
                "subjects": [
                    {
                        "name": "Advanced Engineering Systems",
                        "topics": [
                            {"unit": "Unit 1", "name": "System Modeling & State Space Representation", "difficulty": 4, "est_hours": 2.5, "weightage": 1.4},
                            {"unit": "Unit 2", "name": "Feedback Control & Stability Analysis", "difficulty": 4, "est_hours": 3.0, "weightage": 1.6},
                            {"unit": "Unit 3", "name": "Digital Signal Processing Fundamentals", "difficulty": 3, "est_hours": 2.0, "weightage": 1.2}
                        ]
                    }
                ]
            }, indent=2)

        # Generic explanation or check-in
        return (
            "Planner Agent analyzed your current academic workload. Your plan allocates high-difficulty modules "
            "to peak energy hours with spaced revision intervals leading up to exam day."
        )

    def generate_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools_schema: List[Dict[str, Any]],
        system_prompt: Optional[str] = None
    ) -> LLMResponse:
        """
        Simulate multi-step agent reasoning loop.
        Inspects message history to decide which next tool to call or whether to conclude.
        """
        today = date.today()
        start_str = today.isoformat()
        end_str = (today + timedelta(days=28)).isoformat()

        # Gather previous tool calls and results from conversation history
        tools_called = []
        for msg in messages:
            if msg.get("role") == "tool" or msg.get("tool_name"):
                tools_called.append(msg.get("tool_name") or msg.get("name"))
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg.get("tool_calls", []):
                    name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                    if name:
                        tools_called.append(name)

        # Step 1: If get_free_slots hasn't been called, call it
        if "get_free_slots" not in tools_called:
            return LLMResponse(
                content="I will first inspect the student's available daily study windows and energy levels.",
                tool_calls=[ToolCallRequest(
                    name="get_free_slots",
                    arguments={"start_date": start_str, "end_date": end_str}
                )],
                is_mock=True,
                is_fallback=self.is_fallback,
                provider_name="Mock (Fallback from Gemini)" if self.is_fallback else "Mock LLM",
                model_name="mock",
                fallback_reason=self.fallback_reason
            )

        # Step 2: If create_schedule hasn't been called, call it
        if "create_schedule" not in tools_called:
            return LLMResponse(
                content="Now that I have the student's free slots, I will allocate topics based on exam proximity and difficulty.",
                tool_calls=[ToolCallRequest(
                    name="create_schedule",
                    arguments={"start_date": start_str, "end_date": end_str}
                )],
                is_mock=True,
                is_fallback=self.is_fallback,
                provider_name="Mock (Fallback from Gemini)" if self.is_fallback else "Mock LLM",
                model_name="mock",
                fallback_reason=self.fallback_reason
            )

        # Step 3: If check_conflicts hasn't been called, call it to audit the schedule
        if "check_conflicts" not in tools_called:
            return LLMResponse(
                content="Schedule constructed. Now verifying constraints, daily limits, and revision buffers with check_conflicts.",
                tool_calls=[ToolCallRequest(
                    name="check_conflicts",
                    arguments={}
                )],
                is_mock=True,
                is_fallback=self.is_fallback,
                provider_name="Mock (Fallback from Gemini)" if self.is_fallback else "Mock LLM",
                model_name="mock",
                fallback_reason=self.fallback_reason
            )

        # Inspect latest check_conflicts observation
        last_conflict_data = None
        for msg in reversed(messages):
            if (msg.get("role") == "tool" and (msg.get("tool_name") == "check_conflicts" or msg.get("name") == "check_conflicts")):
                try:
                    c_text = msg.get("content", "{}")
                    last_conflict_data = json.loads(c_text) if isinstance(c_text, str) else c_text
                    break
                except Exception:
                    pass

        # Step 4: Real self-correction loop
        if last_conflict_data and last_conflict_data.get("conflicts_found", False):
            conflicts = last_conflict_data.get("conflicts", [])
            last_msg = messages[-1] if messages else {}
            last_tool = last_msg.get("tool_name") or last_msg.get("name")

            # If a fix was just executed, re-audit with check_conflicts
            if last_tool in ("add_revision_sessions", "compress_plan", "create_schedule"):
                return LLMResponse(
                    content="Fix applied. Re-auditing schedule with check_conflicts to verify resolution.",
                    tool_calls=[ToolCallRequest(
                        name="check_conflicts",
                        arguments={}
                    )],
                    is_mock=True,
                    is_fallback=self.is_fallback,
                    provider_name="Mock (Fallback from Gemini)" if self.is_fallback else "Mock LLM",
                    model_name="mock",
                    fallback_reason=self.fallback_reason
                )

            # Check if missing final revision exists
            missing_rev = next((c for c in conflicts if c.get("type") == "missing_final_revision"), None)
            if missing_rev and tools_called.count("add_revision_sessions") < 2:
                sub_id = missing_rev.get("subject_id")
                return LLMResponse(
                    content="Conflict detected: missing pre-exam revision for subject. Calling add_revision_sessions to resolve.",
                    tool_calls=[ToolCallRequest(
                        name="add_revision_sessions",
                        arguments={"subject_id": sub_id} if sub_id else {}
                    )],
                    is_mock=True,
                    is_fallback=self.is_fallback,
                    provider_name="Mock (Fallback from Gemini)" if self.is_fallback else "Mock LLM",
                    model_name="mock",
                    fallback_reason=self.fallback_reason
                )

            # Check if unscheduled topics exist and panic mode hasn't been tried
            unsched = next((c for c in conflicts if c.get("type") == "unscheduled_topics"), None)
            if unsched and "compress_plan" not in tools_called:
                # Derive subject_id from the first unscheduled topic entry,
                # falling back to the subject with the nearest exam date.
                target_subject_id = None
                unsched_topics_list = unsched.get("topics", [])
                if unsched_topics_list and unsched_topics_list[0].get("subject_id"):
                    target_subject_id = unsched_topics_list[0]["subject_id"]
                if target_subject_id is None:
                    # Fallback: pick the subject with the nearest exam_date
                    from app.db import get_connection
                    with get_connection() as conn:
                        nearest = conn.execute(
                            "SELECT id FROM subjects ORDER BY exam_date ASC LIMIT 1;"
                        ).fetchone()
                        if nearest:
                            target_subject_id = nearest["id"]
                if target_subject_id is None:
                    target_subject_id = 1  # ultimate fallback
                return LLMResponse(
                    content="Unscheduled topics detected. Activating Panic Mode compression on heaviest subject to fit critical topics.",
                    tool_calls=[ToolCallRequest(
                        name="compress_plan",
                        arguments={"subject_id": target_subject_id, "days_left": 4}
                    )],
                    is_mock=True,
                    is_fallback=self.is_fallback,
                    provider_name="Mock (Fallback from Gemini)" if self.is_fallback else "Mock LLM",
                    model_name="mock",
                    fallback_reason=self.fallback_reason
                )

            # Remaining unresolved conflicts (build from unified template)
            from app.tools.scheduling import build_plan_summary
            return LLMResponse(
                content=build_plan_summary(conflicts),
                tool_calls=[],
                finish_reason="stop",
                is_mock=True,
                is_fallback=self.is_fallback,
                provider_name="Mock (Fallback from Gemini)" if self.is_fallback else "Mock LLM",
                model_name="mock",
                fallback_reason=self.fallback_reason
            )

        # Step 5: Verification passed with zero conflicts (build from unified template)
        from app.tools.scheduling import build_plan_summary
        return LLMResponse(
            content=build_plan_summary([]),
            tool_calls=[],
            finish_reason="stop",
            is_mock=True,
            is_fallback=self.is_fallback,
            provider_name="Mock (Fallback from Gemini)" if self.is_fallback else "Mock LLM",
            model_name="mock",
            fallback_reason=self.fallback_reason
        )

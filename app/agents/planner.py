"""Autonomous Planner Agent with self-correcting tool loop and live SSE stream broadcasting."""
import json
import uuid
import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Dict, Any, List, Optional

from app.db import get_db, get_current_llm_mode
from app.llm.gemini import get_llm_provider
from app.tools.registry import get_planner_tools_schema, execute_tool
from app.agents.prompts import PLANNER_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# Global registry of active SSE event queues and history by run_id
AGENT_EVENT_QUEUES: Dict[str, asyncio.Queue] = {}
AGENT_RUN_HISTORY: Dict[str, List[Dict[str, Any]]] = {}

def get_or_create_queue(run_id: str) -> asyncio.Queue:
    """Get or create an SSE broadcast queue for a given run_id."""
    if run_id not in AGENT_EVENT_QUEUES:
        AGENT_EVENT_QUEUES[run_id] = asyncio.Queue()
    return AGENT_EVENT_QUEUES[run_id]

async def push_agent_event(run_id: str, event_type: str, data: Dict[str, Any]) -> None:
    """Push a real-time event to the SSE queue and buffer in history."""
    payload = {"type": event_type, "data": data}
    if run_id not in AGENT_RUN_HISTORY:
        AGENT_RUN_HISTORY[run_id] = []
    AGENT_RUN_HISTORY[run_id].append(payload)

    q = get_or_create_queue(run_id)
    await q.put(payload)

def _save_run_to_db(run_id: str, provider: str, mode: str, status: str,
                    iterations: int, summary: str, is_fallback: bool,
                    events: List[Dict[str, Any]]) -> None:
    """Persist agent run metadata and full event replay to SQLite."""
    try:
        with get_db() as conn:
            conn.execute("""
            INSERT OR REPLACE INTO agent_runs (id, provider, mode, status, iterations, summary, is_fallback, events_json, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                run_id, provider, mode, status, iterations,
                summary, 1 if is_fallback else 0,
                json.dumps(events),
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat()
            ))
    except Exception as e:
        logger.error(f"Failed to persist agent run {run_id}: {e}")

class PlannerAgent:
    """
    Autonomous Planner Agent executing a maximum of 8 tool-calling iterations.
    Supports self-correction, SQLite logging, and non-blocking live SSE streaming.
    Picks the LLM provider fresh on every run (no module caching).
    """

    def __init__(self, run_id: Optional[str] = None):
        self.run_id = run_id or str(uuid.uuid4())
        # Pick provider fresh on every run
        self.llm = get_llm_provider()
        self.tools_schema = get_planner_tools_schema()
        self.max_iterations = 8

    async def run(self, student_context: Optional[str] = None) -> Dict[str, Any]:
        """
        Execute the autonomous agent loop.
        Goal -> Call tool -> Observe result -> Self-correct -> check_conflicts -> Final Answer.
        Guaranteed non-blocking via asyncio.to_thread for both LLM and deterministic tool calls.
        """
        run_id = self.run_id
        today = date.today().isoformat()
        current_mode = get_current_llm_mode()
        provider_label = getattr(self.llm, "model_name", "Mock LLM")
        if current_mode == "live":
            provider_display = f"Gemini ({provider_label})"
        else:
            provider_display = "Mock LLM Provider"

        with get_db() as conn:
            subj_rows = conn.execute("SELECT name, exam_date FROM subjects ORDER BY exam_date;").fetchall()
            top_cnt = conn.execute("SELECT COUNT(*) as c FROM topics;").fetchone()["c"]

        subj_names = [f"'{s['name']}' (Exam: {s['exam_date']})" for s in subj_rows]
        subj_summary = ", ".join(subj_names) if subj_names else "enrolled subjects"

        default_context = (
            f"Current date is {today}. The student is enrolled in {len(subj_rows)} subjects with {top_cnt} syllabus topics: {subj_summary}.\n"
            "All subjects and syllabus topics are ALREADY in the database. DO NOT invent new subjects or fake topics.\n"
            "Your task: call get_free_slots, create_schedule, add_revision_sessions, and check_conflicts to build and verify the student's study timetable."
        )
        user_message = student_context or default_context

        messages: List[Dict[str, Any]] = [
            {"role": "user", "content": user_message}
        ]

        await push_agent_event(run_id, "start", {
            "run_id": run_id,
            "message": "Planner Agent initialized.",
            "provider": provider_display,
            "mode": current_mode
        })

        final_summary = ""
        iterations = 0
        conflicts_resolved = False
        is_fallback_run = False
        fallback_toast_emitted = False
        last_conflict_result = None

        try:
            while iterations < self.max_iterations:
                iterations += 1
                await push_agent_event(run_id, "iteration", {"step": iterations, "max": self.max_iterations})

                # Offload LLM call to worker thread so event loop remains responsive
                llm_response = await asyncio.to_thread(
                    self.llm.generate_with_tools,
                    messages=messages,
                    tools_schema=self.tools_schema,
                    system_prompt=PLANNER_SYSTEM_PROMPT
                )

                # Detect per-run fallback if Live mode failed
                if llm_response.is_fallback:
                    is_fallback_run = True
                    provider_display = "Mock (Fallback from Gemini)"
                    if not fallback_toast_emitted:
                        fallback_toast_emitted = True
                        await push_agent_event(run_id, "fallback", {
                            "message": "Gemini unavailable, used Mock for this run",
                            "reason": llm_response.fallback_reason or "API call error",
                            "provider": provider_display
                        })

                # Record thought / content
                thought = llm_response.content or "Evaluating next scheduling action..."
                await push_agent_event(run_id, "thought", {
                    "step": iterations,
                    "thought": thought,
                    "is_mock": llm_response.is_mock,
                    "is_fallback": llm_response.is_fallback,
                    "provider": provider_display
                })

                # If no tool calls returned, check if audit passed or needs self-correction
                if not llm_response.tool_calls:
                    # Require audit check before concluding
                    if last_conflict_result is None and iterations < self.max_iterations:
                        messages.append({
                            "role": "user",
                            "content": "You must call check_conflicts to audit constraints and pre-exam buffers before concluding."
                        })
                        continue

                    # If conflicts were found, do NOT finish; ask agent to self-correct
                    if last_conflict_result and last_conflict_result.get("conflicts_found", False) and iterations < self.max_iterations:
                        conflicts = last_conflict_result.get("conflicts", [])
                        conflict_descriptions = [c.get("message", str(c)) for c in conflicts]
                        messages.append({
                            "role": "user",
                            "content": (
                                f"check_conflicts detected {len(conflicts)} unresolved conflict(s):\n"
                                + "\n".join(f"- {m}" for m in conflict_descriptions[:4])
                                + "\nYou must resolve these conflicts using tools (e.g. create_schedule, add_revision_sessions, or compress_plan) and then call check_conflicts again."
                            )
                        })
                        continue

                    final_summary = llm_response.content or ""
                    break

                # Execute tool calls requested by the agent
                for tool_call in llm_response.tool_calls:
                    tool_name = tool_call.name
                    tool_args = tool_call.arguments

                    await push_agent_event(run_id, "tool_call", {
                        "step": iterations,
                        "tool": tool_name,
                        "args": tool_args,
                        "provider": provider_display
                    })

                    # Deterministic tool execution offloaded to worker thread
                    tool_result = await asyncio.to_thread(execute_tool, tool_name, tool_args)

                    # Log step into SQLite agent_logs table
                    with get_db() as conn:
                        conn.execute("""
                        INSERT INTO agent_logs (run_id, step, tool_name, args_json, result_json, thought)
                        VALUES (?, ?, ?, ?, ?, ?);
                        """, (
                            run_id,
                            iterations,
                            tool_name,
                            json.dumps(tool_args),
                            json.dumps(tool_result),
                            f"[{provider_display}] {thought}"
                        ))

                    await push_agent_event(run_id, "tool_result", {
                        "step": iterations,
                        "tool": tool_name,
                        "result": tool_result
                    })

                    # Append tool observation to conversation history
                    messages.append({
                        "role": "assistant",
                        "content": f"Calling tool {tool_name} with arguments {json.dumps(tool_args)}",
                        "tool_calls": [{"name": tool_name, "arguments": tool_args}]
                    })
                    messages.append({
                        "role": "tool",
                        "name": tool_name,
                        "tool_name": tool_name,
                        "content": json.dumps(tool_result)
                    })

                    if tool_name == "check_conflicts":
                        last_conflict_result = tool_result
                        conflicts_resolved = not tool_result.get("conflicts_found", False)

                # Yield control briefly to ensure event transmission
                await asyncio.sleep(0.05)

            # End of loop: Build final summary strictly from real last check_conflicts using unified template
            from app.tools.scheduling import check_conflicts_internal, build_plan_summary
            final_audit = await asyncio.to_thread(check_conflicts_internal)
            conflicts_resolved = not final_audit.get("conflicts_found", False)
            remaining_conflicts = final_audit.get("conflicts", [])
            final_summary = build_plan_summary(remaining_conflicts)

            await push_agent_event(run_id, "summary", {
                "run_id": run_id,
                "summary": final_summary,
                "iterations": iterations,
                "conflicts_resolved": conflicts_resolved,
                "provider": provider_display,
                "is_fallback": is_fallback_run
            })
            await push_agent_event(run_id, "done", {
                "status": "completed",
                "provider": provider_display,
                "is_fallback": is_fallback_run
            })

            # Persist full run history to SQLite for page-refresh survival
            _save_run_to_db(
                run_id=run_id, provider=provider_display, mode=current_mode,
                status="completed", iterations=iterations, summary=final_summary,
                is_fallback=is_fallback_run,
                events=AGENT_RUN_HISTORY.get(run_id, [])
            )

            return {
                "status": "success",
                "run_id": run_id,
                "iterations": iterations,
                "summary": final_summary,
                "conflicts_resolved": conflicts_resolved,
                "provider": provider_display,
                "is_fallback": is_fallback_run
            }

        except Exception as exc:
            logger.exception(f"Unhandled error in PlannerAgent.run: {exc}")
            await push_agent_event(run_id, "error", {
                "message": f"Agent encountered an error: {str(exc)}",
                "provider": provider_display
            })
            await push_agent_event(run_id, "done", {
                "status": "failed",
                "provider": provider_display,
                "error": str(exc)
            })

            # Persist failed run too
            _save_run_to_db(
                run_id=run_id, provider=provider_display, mode=current_mode,
                status="failed", iterations=iterations, summary=str(exc),
                is_fallback=is_fallback_run,
                events=AGENT_RUN_HISTORY.get(run_id, [])
            )

            return {"status": "error", "error": str(exc), "run_id": run_id}

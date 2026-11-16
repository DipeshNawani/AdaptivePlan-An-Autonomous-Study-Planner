"""Pydantic models for API request/response validation and tool schemas."""
from datetime import date
from typing import List, Optional, Literal
from pydantic import BaseModel, Field, field_validator

# ----------------- Core Entity Models -----------------

class ProfileModel(BaseModel):
    max_daily_hours: float = Field(..., ge=0.5, le=16.0, description="Maximum study hours per day")
    preferred_time: Literal["morning", "afternoon", "evening", "night"] = "evening"
    break_minutes: int = Field(15, ge=5, le=60, description="Break length in minutes after 90m study")
    burnout_threshold_days: int = Field(5, ge=2, le=14)

class FreeSlotModel(BaseModel):
    weekday: int = Field(..., ge=0, le=6, description="0=Monday, 6=Sunday")
    start_time: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="HH:MM")
    end_time: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="HH:MM")
    energy: Literal["high", "low"] = "high"

    @field_validator("end_time")
    @classmethod
    def validate_time_range(cls, v, values):
        start = values.data.get("start_time")
        if start and v <= start:
            raise ValueError(f"end_time ({v}) must be greater than start_time ({start})")
        return v

class TopicCreate(BaseModel):
    subject_id: Optional[int] = None
    unit: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    difficulty: int = Field(3, ge=1, le=5)
    est_hours: float = Field(2.0, ge=0.25, le=24.0)
    weightage: float = Field(1.0, ge=0.1, le=10.0)
    mastery: float = Field(0.0, ge=0.0, le=1.0)
    status: Literal["pending", "in_progress", "completed"] = "pending"

class SubjectCreate(BaseModel):
    name: str = Field(..., min_length=1)
    exam_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD")
    priority: int = Field(1, ge=1, le=10)
    color: str = "#3B82F6"
    topics: List[TopicCreate] = []

    @field_validator("exam_date")
    @classmethod
    def validate_future_date(cls, v):
        try:
            d = date.fromisoformat(v)
            if d < date.today():
                raise ValueError(f"Exam date ({v}) cannot be in the past")
        except ValueError as e:
            raise ValueError(f"Invalid date: {e}")
        return v

class SetupRequest(BaseModel):
    profile: ProfileModel
    free_slots: List[FreeSlotModel] = []
    subjects: List[SubjectCreate] = []

    @field_validator("subjects")
    @classmethod
    def validate_subjects_and_topics(cls, v):
        if not v:
            raise ValueError("At least one subject must be enrolled")
        for s in v:
            if not s.topics:
                raise ValueError(f"Subject '{s.name}' must have at least one topic")
        return v

class SubjectUpdate(BaseModel):
    name: Optional[str] = None
    exam_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    priority: Optional[int] = Field(None, ge=1, le=10)
    color: Optional[str] = None

    @field_validator("exam_date")
    @classmethod
    def validate_future_date(cls, v):
        if v:
            try:
                d = date.fromisoformat(v)
                if d < date.today():
                    raise ValueError(f"Exam date ({v}) cannot be in the past")
            except ValueError as e:
                raise ValueError(f"Invalid date: {e}")
        return v

class TopicUpdate(BaseModel):
    unit: Optional[str] = None
    name: Optional[str] = None
    difficulty: Optional[int] = Field(None, ge=1, le=5)
    est_hours: Optional[float] = Field(None, ge=0.25, le=24.0)
    weightage: Optional[float] = Field(None, ge=0.1, le=10.0)
    mastery: Optional[float] = Field(None, ge=0.0, le=1.0)
    status: Optional[Literal["pending", "in_progress", "completed"]] = None

class LLMModeRequest(BaseModel):
    mode: Literal["mock", "live"]

class PastPaperWeightageRequest(BaseModel):
    text: str = Field(..., min_length=1)

# ----------------- Plan & Task Models -----------------

class TaskOut(BaseModel):
    id: int
    topic_id: int
    subject_id: int
    subject_name: str
    unit: str
    topic_name: str
    color: str
    type: Literal["study", "revision", "quiz"]
    date: str
    start_time: str
    end_time: str
    duration_hours: float
    status: Literal["pending", "done", "missed", "needs_revision"]
    reason: Optional[str] = None
    priority_score: float = 0.0

class TaskStatusUpdate(BaseModel):
    status: Literal["done", "missed", "needs_revision"]

class ReplanRequest(BaseModel):
    from_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    reason: str = "Missed study targets or schedule adjustment"

class PanicRequest(BaseModel):
    subject_id: int = Field(..., gt=0)
    days_left: int = Field(..., ge=1, le=60)

# ----------------- Quiz Models -----------------

class QuizQuestion(BaseModel):
    question: str
    options: List[str]
    answer: str
    explanation: str

class QuizGenerateRequest(BaseModel):
    topic_id: int = Field(..., gt=0)
    n: int = Field(3, ge=1, le=10)

class QuizSubmitRequest(BaseModel):
    topic_id: int = Field(..., gt=0)
    answers: List[str]
    questions: List[QuizQuestion]

# ----------------- Coach & Agent Models -----------------

class CoachFeedbackOut(BaseModel):
    score: int = Field(..., ge=0, le=100)
    issues: List[str] = []
    suggestions: List[str] = []
    encouragement: str
    burnout_warning: Optional[str] = None

class AgentLogItem(BaseModel):
    id: int
    run_id: str
    step: int
    tool_name: str
    args_json: str
    result_json: str
    thought: Optional[str] = None
    created_at: str

# ----------------- Strict Tool Call Validation Schemas -----------------
# Used to validate LLM tool call arguments and reject invalid data

class ParseSyllabusToolArgs(BaseModel):
    text: Optional[str] = None
    file_id: Optional[str] = None

class EstimateDifficultyToolArgs(BaseModel):
    topic_id: int = Field(..., gt=0)

class GetFreeSlotsToolArgs(BaseModel):
    start_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

class CreateScheduleToolArgs(BaseModel):
    task_ids: Optional[List[int]] = None
    start_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

class CheckConflictsToolArgs(BaseModel):
    pass

class AddRevisionSessionsToolArgs(BaseModel):
    task_id: Optional[int] = Field(None, gt=0)
    subject_id: Optional[int] = Field(None, gt=0)

class MarkProgressToolArgs(BaseModel):
    task_id: int = Field(..., gt=0)
    status: Literal["done", "missed", "needs_revision"]

class ReplanToolArgs(BaseModel):
    from_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    reason: str = Field(..., min_length=1)

class CompressPlanToolArgs(BaseModel):
    subject_id: int = Field(..., gt=0)
    days_left: int = Field(..., ge=1, le=60)

class GenerateQuizToolArgs(BaseModel):
    topic_id: int = Field(..., gt=0)
    n: int = Field(3, ge=1, le=10)

class RecordQuizResultToolArgs(BaseModel):
    topic_id: int = Field(..., gt=0)
    score: float = Field(..., ge=0.0, le=100.0)

class SendReminderToolArgs(BaseModel):
    message: str = Field(..., min_length=1)
    time: str = Field(..., min_length=1)

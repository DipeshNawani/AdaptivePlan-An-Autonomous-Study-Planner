"""Syllabus parsing tool: extracts subjects, units, and topics from text or PDF.
Supports Gemini AI extraction in Live mode and robust rule-based parsing in Mock mode.
"""
import re
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
from pypdf import PdfReader
from app.db import get_db, get_connection, get_current_llm_mode
from app.llm.gemini import get_llm_provider

logger = logging.getLogger(__name__)

def rule_based_syllabus_parser(text: str, subject_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Robust rule-based syllabus parser for offline/mock mode and fast fallback.
    Handles:
    - Newline-separated topics
    - Semicolon-separated topics within units (e.g. "6.2 Applications: Topic 1; Topic 2; Topic 3")
    - Bullets (*, -, •, ·)
    - Numbered items (1., 1.1, 2.a, etc.)
    - Unit/Module/Chapter headers
    """
    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
    current_unit = "Unit 1"
    topics: List[Dict[str, Any]] = []

    unit_header_pattern = re.compile(
        r"^(?:(?:Unit|Module|Chapter)\s*\d+|(?:[\d\.]+\s+[A-Za-z0-9\s]+))\s*[:\-]\s*",
        re.IGNORECASE
    )

    for line in lines:
        lower = line.lower()
        # 1. Check if line is purely a unit/module header without colon
        if (lower.startswith("unit") or lower.startswith("module") or lower.startswith("chapter")) and (":" not in line and "-" not in line):
            current_unit = line.strip()
            continue

        # 2. Check if line starts with a unit header like "Unit 1: ..." or "6.2 Applications: ..."
        m = unit_header_pattern.match(line)
        if m:
            current_unit = m.group(0).strip(":- ")
            content_part = line[m.end():].strip()
        elif ":" in line and any(lower.startswith(k) for k in ("unit", "module", "chapter")):
            parts = line.split(":", 1)
            current_unit = parts[0].strip()
            content_part = parts[1].strip()
        else:
            content_part = line

        # 3. Split content_part into sub-topics if it contains semicolons or bullet characters
        if ";" in content_part:
            raw_subtopics = [p.strip() for p in content_part.split(";") if p.strip()]
        elif re.search(r"\s+[•·\*\-]\s+", content_part):
            raw_subtopics = [p.strip() for p in re.split(r"\s+[•·\*\-]\s+", content_part) if p.strip()]
        else:
            raw_subtopics = [content_part]

        for sub in raw_subtopics:
            clean_name = re.sub(r"^[-*•·\d+.)\s]+", "", sub).strip()
            clean_name = clean_name.rstrip(";., ")
            if len(clean_name) >= 3:
                # Assign sensible difficulty heuristic based on keywords
                lower_sub = clean_name.lower()
                diff = 3
                if any(w in lower_sub for w in ("advanced", "architecture", "consensus", "optimization", "neural", "deep")):
                    diff = 4
                elif any(w in lower_sub for w in ("distributed", "security", "complex", "cryptography", "quantum")):
                    diff = 5
                elif any(w in lower_sub for w in ("intro", "overview", "basics", "fundamental", "history", "definition")):
                    diff = 2

                topics.append({
                    "unit": current_unit,
                    "name": clean_name,
                    "difficulty": diff,
                    "est_hours": 2.0 if diff <= 3 else 2.5,
                    "weightage": 1.0 if diff <= 3 else 1.2
                })

    if not topics:
        topics = [
            {"unit": "Unit 1", "name": "Core Fundamentals", "difficulty": 3, "est_hours": 2.0, "weightage": 1.0},
            {"unit": "Unit 1", "name": "Applied Techniques", "difficulty": 4, "est_hours": 2.5, "weightage": 1.3}
        ]

    subj = subject_name or "Parsed Syllabus Subject"
    return {
        "subject": subj,
        "subjects": [
            {
                "name": subj,
                "topics": topics
            }
        ],
        "topics": topics
    }

def parse_multi_subject_syllabus(raw_text: str) -> List[Dict[str, Any]]:
    """
    Intelligently splits full-curriculum documents (multiple subjects with units and topics).
    Auto-assigns colors, exam dates (relative future dates), difficulty heuristics, and hours.
    """
    from datetime import date, timedelta
    lines = [l.strip() for l in raw_text.splitlines() if l.strip()]
    subjects = []
    current_subject = None
    current_unit = "Unit 1"

    PALETTE = ["#4F46E5", "#059669", "#D97706", "#DC2626", "#7C3AED", "#2563EB", "#DB2777"]
    today = date.today()

    for i, line in enumerate(lines):
        # Lookahead: Check if this line is a subject heading (followed by standalone digit 1 or Unit 1)
        is_subject = False
        if i + 1 < len(lines):
            nxt = lines[i+1].lower()
            if nxt in ("1", "01", "unit 1", "module 1", "chapter 1") or nxt.startswith("unit 1") or nxt.startswith("module 1"):
                if not re.match(r"^(\d+\.|\*|-|•|●)", line) and len(line) < 60:
                    is_subject = True

        if is_subject:
            s_idx = len(subjects)
            color = PALETTE[s_idx % len(PALETTE)]
            exam_date = (today + timedelta(days=14 + (s_idx * 7))).isoformat()
            current_subject = {
                "name": line,
                "color": color,
                "exam_date": exam_date,
                "priority": 1,
                "topics": []
            }
            subjects.append(current_subject)
            current_unit = "Unit 1"
            continue

        if not current_subject:
            s_idx = len(subjects)
            color = PALETTE[s_idx % len(PALETTE)]
            exam_date = (today + timedelta(days=14)).isoformat()
            current_subject = {
                "name": line if len(line) < 40 and not line.isdigit() else "Subject 1",
                "color": color,
                "exam_date": exam_date,
                "priority": 1,
                "topics": []
            }
            subjects.append(current_subject)
            if current_subject["name"] == line:
                continue

        # Standalone unit numbers '1', '2', ..., '6'
        if line.isdigit() and 1 <= int(line) <= 20:
            current_unit = f"Unit {line}"
            continue

        # Check for unit titles like 'Unit 2: ...' or '6. Link Analysis...'
        if re.match(r"^(?:unit|module|chapter)\s*\d+", line, re.I):
            current_unit = line.split(":", 1)[0].strip()
            continue

        if re.match(r"^\d+\.\s+[A-Za-z]+", line) and len(line) < 70 and ":" not in line:
            current_unit = line.strip()
            continue

        # If previous line was a digit, this line is the unit name
        if i > 0 and lines[i-1].isdigit() and len(line) < 80 and not re.match(r"^\d+\.\d+", line):
            current_unit = f"Unit {lines[i-1]}: {line}"
            continue

        # Skip standalone self-learning headers
        if line.lower().startswith("self learning") or line.lower().startswith("self-learning"):
            continue

        # Strip inline "Self Learning Topics: ..." from end of line
        clean_line = re.sub(r"(?i)\s*self[\s\-]learning\s*topics\s*:?.*$", "", line).strip()
        if not clean_line:
            continue

        # Split on semicolons or bullets
        parts = [p.strip() for p in re.split(r";|\s+[•\*\-●]\s+|^[•\*\-●]\s+", clean_line) if p.strip()]
        for part in parts:
            clean_name = re.sub(r"^\d+(\.\d+)*\s*[:\-]?\s*", "", part).strip()
            clean_name = clean_name.rstrip(";., ")
            if len(clean_name) >= 3:
                lower_p = clean_name.lower()
                diff = 3
                if any(w in lower_p for w in ("architecture", "consensus", "smart contract", "mapreduce", "mining", "deep", "transformer", "gan", "vae")):
                    diff = 4
                elif any(w in lower_p for w in ("cryptography", "hyperledger", "security", "optimization", "anomaly", "snark", "eigen")):
                    diff = 5
                elif any(w in lower_p for w in ("intro", "overview", "basics", "fundamentals", "definition", "history")):
                    diff = 2

                current_subject["topics"].append({
                    "unit": current_unit,
                    "name": clean_name,
                    "difficulty": diff,
                    "est_hours": 2.0 if diff <= 3 else 2.5,
                    "weightage": 1.0 if diff <= 3 else 1.2
                })

    return subjects



def parse_syllabus_internal(
    text: Optional[str] = None,
    file_id: Optional[str] = None,
    subject_id: Optional[int] = None,
    subject_name: Optional[str] = None,
    save_to_db: bool = True
) -> Dict[str, Any]:
    """
    Parse syllabus text or PDF file into subjects, units, and topics.
    In Live mode: Uses Gemini API with structured JSON output.
    In Mock mode: Uses deterministic rule-based parser.
    """
    raw_content = ""
    if file_id and Path(file_id).exists():
        try:
            reader = PdfReader(file_id)
            for page in reader.pages:
                raw_content += page.extract_text() or ""
        except Exception as e:
            logger.error(f"Failed to read PDF syllabus: {e}")
            raise ValueError(f"Could not read PDF syllabus: {e}")
    elif text:
        raw_content = text
    else:
        raise ValueError("Either text or file_id must be provided to parse_syllabus")

    mode = get_current_llm_mode()
    data = None

    target_subject_name = subject_name
    if not target_subject_name and subject_id:
        with get_connection() as conn:
            row = conn.execute("SELECT name FROM subjects WHERE id = ?;", (subject_id,)).fetchone()
            if row:
                target_subject_name = row["name"]

    if mode == "live":
        try:
            llm = get_llm_provider()
            subj_clause = f'for the course "{target_subject_name}"' if target_subject_name else ""
            extraction_prompt = (
                f"Extract units and individual study topics {subj_clause} from the following syllabus content. "
                "Break long paragraphs, comma-separated lists, and semicolon-separated items into concise individual topic names. "
                "Return strictly valid JSON format: "
                '{"subjects": [{"name": "' + (target_subject_name or "Syllabus Subject") + '", "topics": [{"unit": "Unit 1", "name": "Topic Name", "difficulty": 3, "est_hours": 2.0, "weightage": 1.0}]}]}. '
                "Content:\n" + raw_content[:4000]
            )
            llm_resp = llm.generate(extraction_prompt, system_prompt="You are an expert syllabus extraction engine. Output strictly valid JSON.")
            cleaned = llm_resp.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            data = json.loads(cleaned.strip())
            if target_subject_name and data.get("subjects"):
                data["subjects"][0]["name"] = target_subject_name
        except Exception as e:
            logger.warning(f"Live Gemini syllabus extraction failed, falling back to rule-based parser: {e}")
            data = None

    # Use rule-based parser if mock mode or if Gemini parsing failed
    if not data or not data.get("subjects") or not data["subjects"][0].get("topics"):
        data = rule_based_syllabus_parser(raw_content, subject_name=target_subject_name)

    subjects_added = 0
    topics_added = 0

    if save_to_db:
        with get_db() as conn:
            for subj in data.get("subjects", []):
                name = subj.get("name", target_subject_name or "New Subject").strip()
                if subject_id:
                    s_id = subject_id
                else:
                    existing = conn.execute("SELECT id FROM subjects WHERE name = ?;", (name,)).fetchone()
                    if existing:
                        s_id = existing["id"]
                    else:
                        from datetime import date, timedelta
                        default_exam = (date.today() + timedelta(days=30)).isoformat()
                        cur = conn.execute("INSERT INTO subjects (name, exam_date, priority) VALUES (?, ?, 1);", (name, default_exam))
                        s_id = cur.lastrowid
                        subjects_added += 1

                for top in subj.get("topics", []):
                    conn.execute("""
                    INSERT INTO topics (subject_id, unit, name, difficulty, est_hours, weightage, mastery, status)
                    VALUES (?, ?, ?, ?, ?, ?, 0.0, 'pending');
                    """, (
                        s_id,
                        top.get("unit", "Unit 1"),
                        top.get("name", "Topic"),
                        top.get("difficulty", 3),
                        top.get("est_hours", 2.0),
                        top.get("weightage", 1.0)
                    ))
                    topics_added += 1

    subjects_list = data.get("subjects", [])
    all_topics = subjects_list[0].get("topics", []) if subjects_list else []

    return {
        "status": "success",
        "mode_used": mode,
        "subjects_added": subjects_added,
        "topics_added": topics_added,
        "extracted_data": data,
        "subjects": subjects_list,
        "topics": all_topics
    }

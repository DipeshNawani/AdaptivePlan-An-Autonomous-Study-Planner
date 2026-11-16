"""Calendar and PDF export services for study plans."""
import io
from datetime import datetime, date
from ics import Calendar, Event
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from app.db import get_connection

def generate_ics_calendar() -> str:
    """Generate an iCalendar (.ics) RFC 5545 string representing all scheduled study tasks."""
    cal = Calendar()

    with get_connection() as conn:
        tasks = conn.execute("""
        SELECT t.id, t.type, t.date, t.start_time, t.end_time, t.reason,
               tp.name as topic_name, tp.unit, s.name as subject_name
        FROM tasks t
        JOIN topics tp ON t.topic_id = tp.id
        JOIN subjects s ON tp.subject_id = s.id
        ORDER BY t.date, t.start_time;
        """).fetchall()

    for task in tasks:
        event = Event()
        event.name = f"[{task['type'].upper()}] {task['subject_name']}: {task['topic_name']}"
        start_dt = datetime.strptime(f"{task['date']} {task['start_time']}", "%Y-%m-%d %H:%M")
        end_dt = datetime.strptime(f"{task['date']} {task['end_time']}", "%Y-%m-%d %H:%M")
        event.begin = start_dt
        event.end = end_dt
        event.description = f"Subject: {task['subject_name']}\nUnit: {task['unit']}\nType: {task['type']}\nNotes: {task['reason'] or 'Scheduled by AdaptivePlan'}"
        cal.events.add(event)

    return cal.serialize()

def generate_pdf_schedule() -> bytes:
    """Generate a formatted PDF timetable of the study plan."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    elements = []

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontSize=20,
        leading=24,
        textColor=colors.HexColor('#1E3A8A'),
        spaceAfter=8
    )
    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Normal'],
        fontSize=10,
        leading=14,
        textColor=colors.HexColor('#4B5563'),
        spaceAfter=14
    )
    cell_style = ParagraphStyle(
        'CellText',
        parent=styles['Normal'],
        fontSize=8,
        leading=10
    )
    header_cell_style = ParagraphStyle(
        'HeaderCellText',
        parent=styles['Normal'],
        fontSize=9,
        leading=11,
        textColor=colors.white,
        fontName='Helvetica-Bold'
    )

    elements.append(Paragraph("AdaptivePlan: Student Study Schedule", title_style))
    elements.append(Paragraph(f"Generated on {date.today().strftime('%B %d, %Y')} | Autonomous Study Planning Agent", subtitle_style))
    elements.append(Spacer(1, 10))

    with get_connection() as conn:
        tasks = conn.execute("""
        SELECT t.date, t.start_time, t.end_time, t.duration_hours, t.type, t.status,
               tp.name as topic_name, tp.unit, s.name as subject_name
        FROM tasks t
        JOIN topics tp ON t.topic_id = tp.id
        JOIN subjects s ON tp.subject_id = s.id
        ORDER BY t.date, t.start_time;
        """).fetchall()

    table_data = [
        [
            Paragraph("Date", header_cell_style),
            Paragraph("Time", header_cell_style),
            Paragraph("Subject", header_cell_style),
            Paragraph("Unit & Topic", header_cell_style),
            Paragraph("Type", header_cell_style),
            Paragraph("Status", header_cell_style)
        ]
    ]

    for t in tasks:
        table_data.append([
            Paragraph(t["date"], cell_style),
            Paragraph(f"{t['start_time']} - {t['end_time']}", cell_style),
            Paragraph(t["subject_name"], cell_style),
            Paragraph(f"{t['unit']}: {t['topic_name']}", cell_style),
            Paragraph(t["type"].capitalize(), cell_style),
            Paragraph(t["status"].capitalize(), cell_style)
        ])

    table = Table(table_data, colWidths=[65, 75, 120, 180, 50, 50])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2563EB')),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('TOPPADDING', (0, 0), (-1, 0), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E5E7EB')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F9FAFB')])
    ]))

    elements.append(table)
    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()

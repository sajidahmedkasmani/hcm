"""Payslip PDF rendering (ReportLab)."""

from __future__ import annotations

import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ..models import Payslip


def _pkr(value) -> str:
    return f"PKR {float(value):,.2f}"


def render_payslip_pdf(company_name: str, p: Payslip) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm, title=f"Payslip {p.run.month}")
    styles = getSampleStyleSheet()
    month_label = datetime.strptime(p.run.month, "%Y-%m").strftime("%B %Y")
    e = p.employee
    story = [
        Paragraph(f"<b>{company_name}</b>", styles["Title"]),
        Paragraph(f"Payslip — {month_label}", styles["Heading2"]),
        Spacer(1, 4 * mm),
    ]
    info = Table(
        [
            ["Employee", e.full_name, "Code", e.employee_code],
            ["Department", e.department.name if e.department else "-", "Status", p.run.status.title()],
            ["Working days", str(p.working_days), "Eligible days", str(p.eligible_days)],
        ],
        colWidths=[30 * mm, 60 * mm, 30 * mm, 50 * mm],
    )
    info.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [info, Spacer(1, 6 * mm)]

    rows = [
        ["Earnings", "Amount", "Deductions", "Amount"],
        ["Basic", _pkr(p.basic), f"Attendance ({p.deduction_days:g} day(s))", _pkr(p.deductions)],
        ["House rent (40%)", _pkr(p.hra), "Income tax", _pkr(p.tax)],
        ["Medical (10%)", _pkr(p.medical), "", ""],
        ["Gross (structure)", _pkr(p.gross), "", ""],
        ["Gross (prorated)", _pkr(p.prorated_gross), "Total deductions", _pkr(p.deductions + p.tax)],
    ]
    table = Table(rows, colWidths=[42 * mm, 40 * mm, 50 * mm, 40 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
        ("ALIGN", (1, 1), (1, -1), "RIGHT"),
        ("ALIGN", (3, 1), (3, -1), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [table, Spacer(1, 6 * mm)]

    detail = (
        f"Absent: {p.absent_days:g} · Unpaid leave: {p.unpaid_leave_days:g} · Half days: {p.half_days:g} "
        f"(x0.5) · Late arrivals: {p.late_count}"
        + (f" · Late penalty: {p.late_penalty_days:g} day(s)" if p.late_penalty_days else "")
        + f"<br/>Per-day rate: {_pkr(p.per_day)} · Annual taxable (gross x 12): {_pkr(p.taxable_annual)}"
    )
    story += [Paragraph(detail, styles["Normal"]), Spacer(1, 6 * mm)]
    net = Table([["NET PAY", _pkr(p.net)]], colWidths=[132 * mm, 40 * mm])
    net.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#ecfdf5")),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 12),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#10b981")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story += [net, Spacer(1, 10 * mm),
              Paragraph("Computer-generated payslip — HCM Lite. Tax slab is illustrative.", styles["Italic"])]
    doc.build(story)
    return buf.getvalue()

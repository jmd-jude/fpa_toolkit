"""Render the MedChron PDF in the FPAmed table format shown in the reference screenshot."""
from __future__ import annotations

from html import escape
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    LongTable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from medchron_schemas import ChronologyEntry, PatientHeader, RunManifest


BOX_VIEWER_TEMPLATE = "https://app.box.com/file/{file_id}?page={page}"


def _page_link(entry: ChronologyEntry, style: ParagraphStyle) -> Paragraph:
    url = BOX_VIEWER_TEMPLATE.format(
        file_id=entry.source_file_id, page=entry.first_page
    )
    return Paragraph(
        f'<link href="{escape(url)}" color="#1f4e79"><u>{escape(entry.page_label)}</u></link>',
        style,
    )


# Hard cap on the medical_information cell to prevent a single entry from
# producing a row taller than a full page. ReportLab's LongTable splits rows
# across pages, but individual flowables can still be too tall; this cap
# keeps the rendering robust. The full text lives in the source Box file,
# which is one click away via the Page # hyperlink.
_MEDINFO_CHAR_CAP = 3500
_MEDINFO_TRUNCATION_MARK = (
    " […truncated for layout; click Page # to view full source]"
)


def _medinfo_paragraph(entry: ChronologyEntry, style: ParagraphStyle) -> Paragraph:
    text = entry.medical_information or ""
    if len(text) > _MEDINFO_CHAR_CAP:
        text = text[:_MEDINFO_CHAR_CAP].rstrip() + _MEDINFO_TRUNCATION_MARK
    text = escape(text)
    text = text.replace("\n\n", "<br/><br/>").replace("\n", "<br/>")
    return Paragraph(text, style)


def _provider_cell(entry: ChronologyEntry, style: ParagraphStyle) -> Paragraph:
    lines = [p for p in (entry.provider, entry.facility) if p]
    return Paragraph("<br/>".join(escape(line) for line in lines) or "&nbsp;", style)


def _dates_cell(entry: ChronologyEntry, style: ParagraphStyle) -> Paragraph:
    if entry.event_date_is_range and entry.event_date_end:
        line1 = entry.event_date.strftime("%m/%d/%Y")
        line2 = entry.event_date_end.strftime("%m/%d/%Y")
        return Paragraph(f"{escape(line1)}<br/>{escape(line2)}", style)
    line1 = entry.event_date.strftime("%m/%d/%Y")
    if entry.event_time:
        return Paragraph(f"{escape(line1)}<br/>{escape(entry.event_time)}", style)
    return Paragraph(escape(line1), style)


def render_medchron_pdf(
    output_path: Path,
    patient: PatientHeader,
    entries: list[ChronologyEntry],
    manifest: RunManifest,
) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.6 * inch,
        title=f"MedChron - {patient.patient_name}",
        author="FPAmed",
    )

    styles = getSampleStyleSheet()
    header_style = ParagraphStyle(
        "header",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
    )
    cell_style = ParagraphStyle(
        "cell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11,
    )
    cell_center = ParagraphStyle(
        "cell_center", parent=cell_style, alignment=1
    )
    table_header_style = ParagraphStyle(
        "table_header",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=12,
        alignment=1,
    )

    story: list = []

    story.append(
        Paragraph(
            f"<b>Patient Name:</b> {escape(patient.patient_name or '')}", header_style
        )
    )
    if patient.dob:
        story.append(
            Paragraph(f"<b>DOB:</b> {escape(patient.dob_label)}", header_style)
        )
    story.append(Spacer(1, 0.2 * inch))

    table_header = [
        Paragraph("Dates", table_header_style),
        Paragraph("Provider", table_header_style),
        Paragraph("Medical Information", table_header_style),
        Paragraph("Page #", table_header_style),
        Paragraph("Bates #", table_header_style),
    ]
    rows: list[list] = [table_header]

    for entry in entries:
        rows.append(
            [
                _dates_cell(entry, cell_center),
                _provider_cell(entry, cell_style),
                _medinfo_paragraph(entry, cell_style),
                _page_link(entry, cell_center),
                Paragraph(escape(entry.page_label), cell_center),
            ]
        )

    if len(rows) == 1:
        rows.append(
            [
                Paragraph("-", cell_center),
                Paragraph("-", cell_style),
                Paragraph(
                    "No clinically significant events were identified in the processed documents.",
                    cell_style,
                ),
                Paragraph("-", cell_center),
                Paragraph("-", cell_center),
            ]
        )

    col_widths = [
        0.95 * inch,
        1.35 * inch,
        3.75 * inch,
        0.85 * inch,
        0.7 * inch,
    ]
    table = LongTable(
        rows,
        colWidths=col_widths,
        repeatRows=1,
        splitByRow=1,
        splitInRow=1,
    )
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f2f2")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(table)

    if manifest.unprocessed:
        story.append(PageBreak())
        story.append(
            Paragraph(
                "<b>Unprocessed Files Manifest</b> - these files could not be processed and are therefore NOT reflected in the chronology above. Per FPAmed requirement 1.3, they are surfaced here rather than silently omitted.",
                header_style,
            )
        )
        story.append(Spacer(1, 0.15 * inch))
        unp_rows = [[
            Paragraph("File ID", table_header_style),
            Paragraph("File Name", table_header_style),
            Paragraph("Reason", table_header_style),
        ]]
        for u in manifest.unprocessed:
            unp_rows.append(
                [
                    Paragraph(escape(u.file_id), cell_style),
                    Paragraph(escape(u.file_name), cell_style),
                    Paragraph(escape(u.reason), cell_style),
                ]
            )
        unp_table = Table(
            unp_rows,
            colWidths=[1.2 * inch, 3.2 * inch, 3.1 * inch],
            repeatRows=1,
        )
        unp_table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f2f2")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(unp_table)

    coverage_pct = manifest.overall_coverage_ratio() * 100
    story.append(Spacer(1, 0.3 * inch))
    story.append(
        Paragraph(
            f"<font size=7 color='#666666'>Run ID: {manifest.run_id} &nbsp;|&nbsp; "
            f"Coverage: {coverage_pct:.2f}% &nbsp;|&nbsp; "
            f"Entries: {len(entries)} &nbsp;|&nbsp; "
            f"Source folder: {manifest.source_folder_id} &nbsp;|&nbsp; "
            f"As-user: {manifest.as_user_id}</font>",
            styles["Normal"],
        )
    )

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return output_path


def _footer(canvas, doc) -> None:  # pragma: no cover - visual only
    canvas.saveState()
    canvas.setFont("Helvetica", 9)
    canvas.drawCentredString(letter[0] / 2.0, 0.35 * inch, f"Page {doc.page}")
    canvas.restoreState()

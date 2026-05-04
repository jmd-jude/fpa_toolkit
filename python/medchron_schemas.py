"""Pydantic models used across the MedChron pipeline."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ChronologyEntry(BaseModel):
    """A single row in the chronology table."""

    event_date: date = Field(
        ..., description="Date the clinically significant event occurred."
    )
    event_date_is_range: bool = Field(
        default=False,
        description="True if the entry spans multiple dates (e.g. a hospital stay).",
    )
    event_date_end: date | None = Field(
        default=None,
        description="End of the date range. Only populated if event_date_is_range is True.",
    )
    event_time: str | None = Field(
        default=None,
        description="Optional time-of-day as shown on the record, e.g. '10:32'.",
    )
    provider: str | None = Field(
        default=None,
        description="Attending provider name with credentials, e.g. 'Peter Remedios, M.D.'.",
    )
    facility: str | None = Field(
        default=None,
        description="Clinic/hospital/facility name, e.g. 'Kaiser Permanente'.",
    )
    medical_information: str = Field(
        ...,
        description=(
            "Faithful, structured summary of the clinical information on the page(s). "
            "Preserve labeled sections (Subjective, Assessment, etc.) when present."
        ),
    )
    first_page: int = Field(..., ge=1, description="First page in the source file.")
    last_page: int = Field(..., ge=1, description="Last page in the source file.")
    source_file_id: str = Field(..., description="Box file id for the source.")
    source_file_name: str = Field(..., description="Original Box file name.")
    is_clinically_significant: bool = Field(
        default=True,
        description="Filter flag; non-significant rows are dropped before render.",
    )
    ai_confidence: float = Field(
        default=1.0, ge=0.0, le=1.0, description="AI self-reported confidence."
    )

    @field_validator("event_date", "event_date_end", mode="before")
    @classmethod
    def _coerce_date(cls, v: Any) -> Any:
        if v is None or isinstance(v, date):
            return v
        if isinstance(v, str):
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%d/%m/%Y"):
                try:
                    return datetime.strptime(v, fmt).date()
                except ValueError:
                    continue
        return v

    @property
    def page_label(self) -> str:
        """Human display used in the Page # and Bates # columns."""
        if self.first_page == self.last_page:
            return f"{self.first_page}"
        return f"{self.first_page}-{self.last_page}"

    @property
    def date_label(self) -> str:
        if self.event_date_is_range and self.event_date_end:
            return (
                f"{self.event_date.strftime('%m/%d/%Y')}-"
                f"{self.event_date_end.strftime('%m/%d/%Y')}"
            )
        base = self.event_date.strftime("%m/%d/%Y")
        return f"{base}\n{self.event_time}" if self.event_time else base

    @property
    def provider_label(self) -> str:
        parts = [p for p in (self.provider, self.facility) if p]
        return " - ".join(parts)


class PatientHeader(BaseModel):
    """Header shown at the top of the chronology PDF."""

    patient_name: str = Field(..., description="Full name or redacted initials.")
    dob: date | None = Field(default=None, description="Date of birth (MM/DD/YYYY).")

    @field_validator("dob", mode="before")
    @classmethod
    def _coerce_dob(cls, v: Any) -> Any:
        if v is None or isinstance(v, date):
            return v
        if isinstance(v, str):
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
                try:
                    return datetime.strptime(v, fmt).date()
                except ValueError:
                    continue
        return v

    @property
    def dob_label(self) -> str:
        return self.dob.strftime("%m/%d/%Y") if self.dob else ""


class PageCoverage(BaseModel):
    """Tracks which pages of a given Box file have been processed."""

    file_id: str
    file_name: str
    total_pages: int
    processed_pages: set[int] = Field(default_factory=set)
    failed_pages: dict[int, str] = Field(default_factory=dict)
    skipped_pages: set[int] = Field(default_factory=set)

    def mark_processed(self, start: int, end: int) -> None:
        for p in range(start, end + 1):
            self.processed_pages.add(p)
            self.failed_pages.pop(p, None)

    def mark_failed(self, start: int, end: int, reason: str) -> None:
        for p in range(start, end + 1):
            if p not in self.processed_pages:
                self.failed_pages[p] = reason

    def mark_skipped(self, start: int, end: int) -> None:
        for p in range(start, end + 1):
            self.skipped_pages.add(p)

    def missing_pages(self) -> list[int]:
        accounted = self.processed_pages | self.skipped_pages
        return sorted(
            p for p in range(1, self.total_pages + 1) if p not in accounted
        )

    def coverage_ratio(self) -> float:
        if self.total_pages == 0:
            return 1.0
        return (len(self.processed_pages) + len(self.skipped_pages)) / self.total_pages


class UnprocessedFile(BaseModel):
    file_id: str
    file_name: str
    reason: str


class RunManifest(BaseModel):
    """Summary of a full MedChron run for audit + reproducibility."""

    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    source_folder_id: str
    as_user_id: str
    patient: PatientHeader | None = None
    coverage: list[PageCoverage] = Field(default_factory=list)
    unprocessed: list[UnprocessedFile] = Field(default_factory=list)
    total_entries: int = 0
    output_box_folder_id: str | None = None
    output_file_ids: dict[str, str] = Field(default_factory=dict)

    def overall_coverage_ratio(self) -> float:
        total = sum(c.total_pages for c in self.coverage) or 1
        accounted = sum(len(c.processed_pages) + len(c.skipped_pages) for c in self.coverage)
        return accounted / total

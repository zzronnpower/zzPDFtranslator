from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class JobStatus(str, Enum):
    pending = "pending"
    estimating = "estimating"
    ready = "ready"
    translating = "translating"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


@dataclass
class TextSegment:
    page_index: int
    rect: tuple[float, float, float, float]
    text: str
    font_size: float


@dataclass
class UploadedFile:
    file_id: str
    original_name: str
    input_path: str | None
    uploaded_at: datetime
    page_count: int = 0
    segments: list[TextSegment] = field(default_factory=list)
    source_tokens: int = 0
    extraction_mode: str = "text"
    ocr_pages: int = 0
    ocr_preset: str = "balanced"


@dataclass
class EstimateResult:
    model: str
    source_tokens: int
    estimated_output_tokens: int
    estimated_cost_usd: float


@dataclass
class TranslationJob:
    job_id: str
    file_id: str
    model: str
    accepted_estimate_usd: float
    page_from: int
    page_to: int
    status: JobStatus = JobStatus.pending
    progress: float = 0.0
    message: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    output_path: str | None = None
    downloaded_path: str | None = None
    translated_filename: str | None = None
    estimated_cost_usd: float = 0.0
    actual_input_tokens: int = 0
    actual_output_tokens: int = 0
    actual_cost_usd: float = 0.0
    extraction_mode: str = "text"
    selected_segments: int = 0
    ocr_preset: str = "balanced"
    phase: str = "pending"
    phase_progress: float = 0.0
    last_progress_at: datetime = field(default_factory=datetime.utcnow)
    phase_started_at: datetime = field(default_factory=datetime.utcnow)
    cancelled: bool = False
    timeout_reason: str | None = None
    retry_suggestion_page_from: int | None = None
    translate_duration_seconds: float = 0.0
    render_duration_seconds: float = 0.0
    save_duration_seconds: float = 0.0

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime
from threading import Lock
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.config import MODEL_PRICING, settings
from app.models import JobStatus, TranslationJob, UploadedFile
from app.services.dev_autolog import ensure_dev_tracking_files, record_code_snapshot_if_changed
from app.services.estimator import calculate_cost_usd, estimate_from_source_tokens
from app.services.ocr_extractor import extract_text_segments_with_ocr
from app.services.openai_translator import get_openai_client, translate_batch
from app.services.pdf_translator import count_tokens, extract_text_segments, write_translated_pdf


app = FastAPI(title=settings.app_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class EstimateRequest(BaseModel):
    file_id: str
    model: str
    page_from: int | None = Field(default=None, ge=1)
    page_to: int | None = Field(default=None, ge=1)


class TranslateRequest(BaseModel):
    file_id: str
    model: str
    accepted_estimate_usd: float = Field(..., ge=0)
    accept_estimate: bool
    page_from: int | None = Field(default=None, ge=1)
    page_to: int | None = Field(default=None, ge=1)


uploaded_files: dict[str, UploadedFile] = {}
translation_jobs: dict[str, TranslationJob] = {}
state_lock = Lock()
history_lock = Lock()

OCR_PRESETS: dict[str, dict[str, Any]] = {
    "fast": {"dpi": 170, "min_confidence": 32, "label": "Fast"},
    "balanced": {"dpi": 220, "min_confidence": 25, "label": "Balanced"},
    "quality": {"dpi": 280, "min_confidence": 20, "label": "Quality"},
}
OCR_TIMEOUT_MULTIPLIERS: dict[str, float] = {
    "fast": 0.85,
    "balanced": 1.0,
    "quality": 1.35,
}
RUNNING_PHASES = {"translating", "rendering", "saving"}


def _ensure_storage_dirs() -> None:
    for folder in (
        settings.storage_root,
        settings.input_dir,
        settings.output_dir,
        settings.downloaded_dir,
        settings.temp_dir,
        settings.logs_dir,
    ):
        os.makedirs(folder, exist_ok=True)


def _ensure_history_log_file() -> None:
    if not os.path.exists(settings.history_log_path):
        with open(settings.history_log_path, "a", encoding="utf-8"):
            pass


def _cleanup_startup_storage() -> None:
    for folder in (settings.input_dir, settings.output_dir, settings.temp_dir):
        if not os.path.exists(folder):
            continue
        for name in os.listdir(folder):
            file_path = os.path.join(folder, name)
            if not os.path.isfile(file_path):
                continue
            try:
                os.remove(file_path)
            except OSError:
                pass


@app.on_event("startup")
async def on_startup() -> None:
    _ensure_storage_dirs()
    _cleanup_startup_storage()
    _ensure_history_log_file()
    ensure_dev_tracking_files()
    record_code_snapshot_if_changed(event="api_startup")
    asyncio.get_running_loop().create_task(_job_watchdog_loop())


def _normalize_ocr_preset(raw_preset: str | None) -> str:
    preset = (raw_preset or "balanced").strip().lower()
    if preset not in OCR_PRESETS:
        return "balanced"
    return preset


def _phase_timeout_seconds(job: TranslationJob, phase: str) -> int:
    multiplier = OCR_TIMEOUT_MULTIPLIERS.get(job.ocr_preset, 1.0)
    if phase == "translating":
        return max(300, int(settings.job_timeout_translate_seconds * multiplier))
    if phase == "rendering":
        return max(300, int(settings.job_timeout_render_seconds * multiplier))
    if phase == "saving":
        return max(120, int(settings.job_timeout_save_seconds * multiplier))
    return max(300, int(settings.job_timeout_translate_seconds * multiplier))


def _set_job_phase(job_id: str, phase: str, *, phase_progress: float = 0.0, message: str | None = None) -> None:
    now = datetime.utcnow()
    with state_lock:
        job = translation_jobs.get(job_id)
        if not job:
            return
        if job.phase != phase:
            job.phase_started_at = now
        job.phase = phase
        job.phase_progress = max(0.0, min(100.0, phase_progress))
        job.last_progress_at = now
        if message is not None:
            job.message = message
        job.updated_at = now


def _touch_job_heartbeat(job_id: str) -> None:
    with state_lock:
        job = translation_jobs.get(job_id)
        if not job:
            return
        now = datetime.utcnow()
        job.last_progress_at = now
        job.updated_at = now


def _is_job_cancelled(job_id: str) -> bool:
    with state_lock:
        job = translation_jobs.get(job_id)
        if not job:
            return True
        return bool(job.cancelled)


def _is_job_aborted(job_id: str) -> bool:
    with state_lock:
        job = translation_jobs.get(job_id)
        if not job:
            return True
        return bool(job.cancelled or job.status in {JobStatus.failed, JobStatus.cancelled})


def _resolve_retry_page(job: TranslationJob) -> int:
    if job.page_to <= job.page_from:
        return job.page_from
    total_pages = job.page_to - job.page_from + 1
    if total_pages <= 1:
        return job.page_from

    phase_progress = max(0.0, min(100.0, job.phase_progress))
    if job.phase == "translating":
        completed_pages = int((phase_progress / 100.0) * total_pages)
    elif job.phase == "rendering":
        completed_pages = int((phase_progress / 100.0) * total_pages)
    else:
        completed_pages = 0

    return min(job.page_to, max(job.page_from, job.page_from + completed_pages))


async def _job_watchdog_loop() -> None:
    while True:
        now = datetime.utcnow()
        with state_lock:
            for job in translation_jobs.values():
                if job.status != JobStatus.translating:
                    continue
                if job.phase not in RUNNING_PHASES:
                    continue
                elapsed = (now - job.last_progress_at).total_seconds()
                if elapsed <= _phase_timeout_seconds(job, job.phase):
                    continue
                job.status = JobStatus.failed
                job.timeout_reason = (
                    f"Watchdog timeout: no progress for {int(elapsed)}s during phase '{job.phase}'."
                )
                job.message = f"Translation failed: {job.timeout_reason}"
                job.retry_suggestion_page_from = _resolve_retry_page(job)
                job.updated_at = now
        await asyncio.sleep(5)


def _append_history_entry(entry: dict) -> None:
    line = json.dumps(entry, ensure_ascii=False)
    with history_lock:
        with open(settings.history_log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _read_history_entries(limit: int = 200) -> list[dict]:
    if not os.path.exists(settings.history_log_path):
        return []

    with history_lock:
        with open(settings.history_log_path, "r", encoding="utf-8") as f:
            rows = [line.strip() for line in f if line.strip()]

    entries: list[dict] = []
    for raw in reversed(rows):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        entries.append(parsed)
        if len(entries) >= limit:
            break
    return entries


def _history_summary(entries: list[dict]) -> dict:
    done_entries = [entry for entry in entries if entry.get("status") == JobStatus.done]
    failed_entries = [entry for entry in entries if entry.get("status") == JobStatus.failed]
    cancelled_entries = [entry for entry in entries if entry.get("status") == JobStatus.cancelled]
    total_actual_cost = sum(float(entry.get("actual_cost_usd", 0.0) or 0.0) for entry in done_entries)
    total_estimated_cost = sum(float(entry.get("estimated_cost_usd", 0.0) or 0.0) for entry in done_entries)
    total_lines = sum(int(entry.get("selected_segments", 0) or 0) for entry in done_entries)
    total_output_size = sum(int(entry.get("output_size_bytes", 0) or 0) for entry in done_entries)
    avg_duration = 0.0
    avg_translate_duration = 0.0
    avg_render_duration = 0.0
    avg_save_duration = 0.0
    if done_entries:
        avg_duration = sum(float(entry.get("duration_seconds", 0.0) or 0.0) for entry in done_entries) / len(done_entries)
        avg_translate_duration = (
            sum(float(entry.get("translate_duration_seconds", 0.0) or 0.0) for entry in done_entries) / len(done_entries)
        )
        avg_render_duration = (
            sum(float(entry.get("render_duration_seconds", 0.0) or 0.0) for entry in done_entries) / len(done_entries)
        )
        avg_save_duration = (
            sum(float(entry.get("save_duration_seconds", 0.0) or 0.0) for entry in done_entries) / len(done_entries)
        )

    return {
        "total_jobs": len(entries),
        "successful_jobs": len(done_entries),
        "failed_jobs": len(failed_entries),
        "cancelled_jobs": len(cancelled_entries),
        "total_actual_cost_usd": round(total_actual_cost, 6),
        "total_estimated_cost_usd": round(total_estimated_cost, 6),
        "total_lines_translated": total_lines,
        "total_output_size_bytes": total_output_size,
        "average_duration_seconds": round(avg_duration, 2),
        "average_translate_duration_seconds": round(avg_translate_duration, 2),
        "average_render_duration_seconds": round(avg_render_duration, 2),
        "average_save_duration_seconds": round(avg_save_duration, 2),
    }


def _record_translation_history(
    *,
    job: TranslationJob,
    original_filename: str,
    selected_segments: int,
    input_size_bytes: int,
    output_size_bytes: int,
) -> None:
    finished_at = datetime.utcnow()
    duration_seconds = max(0.0, (finished_at - job.created_at).total_seconds())
    entry = {
        "job_id": job.job_id,
        "status": job.status,
        "model": job.model,
        "original_filename": original_filename,
        "translated_filename": job.translated_filename,
        "page_from": job.page_from,
        "page_to": job.page_to,
        "selected_pages": max(0, job.page_to - job.page_from + 1),
        "selected_segments": selected_segments,
        "extraction_mode": job.extraction_mode,
        "ocr_preset": job.ocr_preset,
        "phase_last": job.phase,
        "phase_progress": round(job.phase_progress, 2),
        "cancelled_by_user": bool(job.status == JobStatus.cancelled),
        "timeout_reason": job.timeout_reason,
        "retry_suggestion_page_from": job.retry_suggestion_page_from,
        "estimated_cost_usd": round(float(job.estimated_cost_usd), 6),
        "accepted_estimate_usd": round(float(job.accepted_estimate_usd), 6),
        "actual_cost_usd": round(float(job.actual_cost_usd), 6),
        "actual_input_tokens": int(job.actual_input_tokens),
        "actual_output_tokens": int(job.actual_output_tokens),
        "input_size_bytes": int(input_size_bytes),
        "output_size_bytes": int(output_size_bytes),
        "message": job.message,
        "created_at": job.created_at.isoformat() + "Z",
        "finished_at": finished_at.isoformat() + "Z",
        "duration_seconds": round(duration_seconds, 2),
        "translate_duration_seconds": round(job.translate_duration_seconds, 2),
        "render_duration_seconds": round(job.render_duration_seconds, 2),
        "save_duration_seconds": round(job.save_duration_seconds, 2),
    }
    _append_history_entry(entry)


def _validate_model(model: str) -> None:
    if model not in MODEL_PRICING:
        raise HTTPException(status_code=400, detail=f"Unsupported model: {model}")


def _resolve_page_range(uploaded: UploadedFile, page_from: int | None, page_to: int | None) -> tuple[int, int]:
    resolved_from = 1 if page_from is None else page_from
    resolved_to = uploaded.page_count if page_to is None else page_to
    if resolved_from > resolved_to:
        raise HTTPException(status_code=400, detail="page_from cannot be greater than page_to")
    if resolved_to > uploaded.page_count:
        raise HTTPException(
            status_code=400,
            detail=f"Requested page range exceeds document size ({uploaded.page_count} pages).",
        )
    return resolved_from, resolved_to


def _segments_for_page_range(uploaded: UploadedFile, page_from: int, page_to: int) -> list:
    start_index = page_from - 1
    end_index = page_to - 1
    return [segment for segment in uploaded.segments if start_index <= segment.page_index <= end_index]


def _should_use_ocr_fallback(page_count: int, segment_count: int) -> bool:
    if not settings.ocr_enabled:
        return False
    if segment_count <= settings.ocr_fallback_segment_threshold:
        return True
    if page_count >= 80 and (segment_count / max(1, page_count)) < 0.08:
        return True
    return False


def _source_tokens_for_segments(model: str, segments: list) -> int:
    if not segments:
        return 0
    joined_text = "\n".join(segment.text for segment in segments)
    return count_tokens(model, joined_text)


def _safe_stem(filename: str) -> str:
    stem = os.path.splitext(filename)[0].strip()
    normalized = re.sub(r"[^A-Za-z0-9._ -]+", "_", stem)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.strip("._-") or "translated"


def _translated_filename_from_original(original_name: str) -> str:
    safe_stem = _safe_stem(original_name)
    return f"Translated_{safe_stem}.pdf"


def _resolve_downloaded_path(original_name: str) -> tuple[str, str]:
    base_name = _translated_filename_from_original(original_name)
    base_stem, ext = os.path.splitext(base_name)
    candidate = os.path.join(settings.downloaded_dir, base_name)
    if not os.path.exists(candidate):
        return candidate, base_name

    version = 2
    while True:
        version_name = f"{base_stem}_v{version}{ext}"
        candidate = os.path.join(settings.downloaded_dir, version_name)
        if not os.path.exists(candidate):
            return candidate, version_name
        version += 1


def _likely_needs_translation(source: str, translated: str) -> bool:
    src = source.strip()
    dst = translated.strip()
    if not src:
        return False
    if len(src) < 24:
        return False
    if not re.search(r"[A-Za-z]", src):
        return False
    return src == dst


def _translate_batch_with_retry(client, model: str, texts: list[str], target_language: str) -> tuple[list[str], int, int]:
    if not texts:
        return [], 0, 0

    resolved: dict[int, str] = {}
    total_prompt_tokens = 0
    total_completion_tokens = 0
    pending: list[tuple[list[tuple[int, str]], int]] = [([(i, text) for i, text in enumerate(texts)], 0)]

    while pending:
        chunk, attempt = pending.pop(0)
        if not chunk:
            continue

        local_texts = [text for _, text in chunk]

        try:
            translated_batch, prompt_tokens, completion_tokens, missing_ids = translate_batch(
                client=client,
                model=model,
                texts=local_texts,
                target_language=target_language,
            )
        except Exception as exc:
            if len(chunk) == 1:
                if attempt < 2:
                    pending.insert(0, (chunk, attempt + 1))
                    continue
                raise RuntimeError(f"Translator failed on single item after retries: {exc}") from exc

            midpoint = len(chunk) // 2
            pending.insert(0, (chunk[midpoint:], 0))
            pending.insert(0, (chunk[:midpoint], 0))
            continue

        total_prompt_tokens += prompt_tokens
        total_completion_tokens += completion_tokens

        missing_set = set(missing_ids)
        missing_chunk: list[tuple[int, str]] = []
        for local_idx, (original_idx, original_text) in enumerate(chunk):
            if local_idx in missing_set:
                missing_chunk.append((original_idx, original_text))
            else:
                resolved[original_idx] = translated_batch[local_idx]

        if not missing_chunk:
            continue

        if len(missing_chunk) == 1:
            if attempt < 2:
                pending.insert(0, (missing_chunk, attempt + 1))
                continue
            missing_id = missing_chunk[0][0]
            raise RuntimeError(f"Translator returned incomplete batch. Missing item ids: [{missing_id}]")

        if len(missing_chunk) == len(chunk):
            midpoint = len(chunk) // 2
            pending.insert(0, (chunk[midpoint:], 0))
            pending.insert(0, (chunk[:midpoint], 0))
            continue

        pending.insert(0, (missing_chunk, 0))

    missing_final = [i for i in range(len(texts)) if i not in resolved]
    if missing_final:
        raise RuntimeError(f"Translator returned incomplete batch. Missing item ids: {missing_final}")

    ordered = [resolved[i] for i in range(len(texts))]
    return ordered, total_prompt_tokens, total_completion_tokens


def _next_batch_end(segments: list, start_index: int, model: str) -> int:
    max_items = max(1, settings.translation_batch_max_items)
    max_tokens = max(400, settings.translation_batch_max_tokens)
    token_total = 0
    end = start_index

    while end < len(segments) and (end - start_index) < max_items:
        text = segments[end].text
        token_estimate = count_tokens(model, text) + 24
        if end > start_index and token_total + token_estimate > max_tokens:
            break
        token_total += token_estimate
        end += 1

    return max(start_index + 1, end)


def _build_translation_chunks(segments: list, model: str) -> list[tuple[int, int, int, list[str]]]:
    chunks: list[tuple[int, int, int, list[str]]] = []
    start = 0
    chunk_index = 0
    while start < len(segments):
        end = _next_batch_end(segments, start, model)
        chunk_texts = [segment.text for segment in segments[start:end]]
        chunks.append((chunk_index, start, end, chunk_texts))
        chunk_index += 1
        start = end
    return chunks


def _translate_chunk_worker(model: str, texts: list[str], target_language: str) -> tuple[list[str], int, int]:
    client = get_openai_client()
    return _translate_batch_with_retry(
        client=client,
        model=model,
        texts=texts,
        target_language=target_language,
    )


def _list_downloaded_files() -> list[dict]:
    items: list[dict] = []

    if os.path.exists(settings.downloaded_dir):
        for filename in os.listdir(settings.downloaded_dir):
            if not filename.lower().endswith(".pdf"):
                continue
            file_path = os.path.join(settings.downloaded_dir, filename)
            if not os.path.isfile(file_path):
                continue
            stat = os.stat(file_path)
            items.append(
                {
                    "filename": filename,
                    "size_bytes": stat.st_size,
                    "updated_at": datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z",
                    "download_url": f"/api/downloaded/{filename}",
                }
            )

    if items:
        items.sort(key=lambda row: row["updated_at"], reverse=True)
        return items

    if os.path.exists(settings.output_dir):
        for filename in os.listdir(settings.output_dir):
            if not filename.lower().endswith(".pdf"):
                continue
            job_id = os.path.splitext(filename)[0]
            if not re.fullmatch(r"[0-9a-f-]{36}", job_id):
                continue
            file_path = os.path.join(settings.output_dir, filename)
            if not os.path.isfile(file_path):
                continue
            stat = os.stat(file_path)
            items.append(
                {
                    "filename": f"translated_{job_id}.pdf",
                    "size_bytes": stat.st_size,
                    "updated_at": datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z",
                    "download_url": f"/api/download/{job_id}",
                }
            )

    items.sort(key=lambda row: row["updated_at"], reverse=True)
    return items


@app.get("/")
def get_index() -> FileResponse:
    return FileResponse("app/web/index.html")


@app.get("/app.js")
def get_app_js() -> FileResponse:
    return FileResponse("app/web/app.js", media_type="application/javascript")


@app.get("/styles.css")
def get_styles() -> FileResponse:
    return FileResponse("app/web/styles.css", media_type="text/css")


@app.get("/api/models")
def list_models() -> dict:
    return {
        "default_model": settings.default_model,
        "openai_api_key_configured": bool(settings.openai_api_key),
        "default_ocr_preset": "balanced",
        "ocr_presets": [
            {
                "id": preset_id,
                "label": cfg["label"],
                "dpi": cfg["dpi"],
                "min_confidence": cfg["min_confidence"],
            }
            for preset_id, cfg in OCR_PRESETS.items()
        ],
        "models": [
            {
                "id": model,
                "input_per_million": pricing.input_per_million,
                "output_per_million": pricing.output_per_million,
            }
            for model, pricing in MODEL_PRICING.items()
        ],
    }


@app.post("/api/upload")
async def upload_pdf(file: UploadFile = File(...), ocr_preset: str = Form("balanced")) -> dict:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a PDF file.")

    file_id = str(uuid.uuid4())
    input_path = os.path.join(settings.input_dir, f"{file_id}.pdf")
    content = await file.read()
    with open(input_path, "wb") as f:
        f.write(content)

    try:
        page_count, segments, source_tokens = await asyncio.to_thread(extract_text_segments, input_path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not parse PDF: {exc}") from exc

    preset = _normalize_ocr_preset(ocr_preset)
    extraction_mode = "text"
    ocr_pages = 0
    if _should_use_ocr_fallback(page_count, len(segments)):
        ocr_cfg = OCR_PRESETS[preset]
        ocr_dpi = int(ocr_cfg["dpi"])
        ocr_conf = int(ocr_cfg["min_confidence"])
        try:
            ocr_page_count, ocr_segments, ocr_source_tokens, ocr_pages = await asyncio.to_thread(
                extract_text_segments_with_ocr,
                input_path,
                dpi=ocr_dpi,
                lang=settings.ocr_language,
                min_confidence=ocr_conf,
            )
            if len(ocr_segments) > len(segments):
                page_count = ocr_page_count
                segments = ocr_segments
                source_tokens = ocr_source_tokens
                extraction_mode = "ocr"
        except Exception:
            extraction_mode = "text"

    if len(segments) == 0:
        raise HTTPException(
            status_code=400,
            detail=(
                "Could not extract readable text from this PDF. "
                "If this is a scanned document, enable OCR settings and try again."
            ),
        )

    uploaded = UploadedFile(
        file_id=file_id,
        original_name=file.filename,
        input_path=input_path,
        uploaded_at=datetime.utcnow(),
        page_count=page_count,
        segments=segments,
        source_tokens=source_tokens,
        extraction_mode=extraction_mode,
        ocr_pages=ocr_pages,
        ocr_preset=preset,
    )

    with state_lock:
        uploaded_files[file_id] = uploaded

    return {
        "file_id": file_id,
        "filename": file.filename,
        "pages": page_count,
        "segments": len(segments),
        "source_tokens": source_tokens,
        "extraction_mode": extraction_mode,
        "ocr_pages": ocr_pages,
        "ocr_preset": preset,
    }


@app.post("/api/estimate")
def estimate_cost(req: EstimateRequest) -> dict:
    _validate_model(req.model)
    with state_lock:
        uploaded = uploaded_files.get(req.file_id)
    if not uploaded:
        raise HTTPException(status_code=404, detail="file_id not found")

    page_from, page_to = _resolve_page_range(uploaded, req.page_from, req.page_to)
    selected_segments = _segments_for_page_range(uploaded, page_from, page_to)
    source_tokens = _source_tokens_for_segments(req.model, selected_segments)
    estimate = estimate_from_source_tokens(req.model, source_tokens)
    return {
        "file_id": req.file_id,
        "model": estimate.model,
        "page_from": page_from,
        "page_to": page_to,
        "selected_pages": page_to - page_from + 1,
        "selected_segments": len(selected_segments),
        "source_tokens": estimate.source_tokens,
        "estimated_output_tokens": estimate.estimated_output_tokens,
        "estimated_cost_usd": estimate.estimated_cost_usd,
    }


def _set_job_state(job_id: str, **kwargs) -> None:
    with state_lock:
        job = translation_jobs[job_id]
        previous_phase = job.phase
        for key, value in kwargs.items():
            setattr(job, key, value)
        now = datetime.utcnow()
        if "phase" in kwargs and kwargs["phase"] != previous_phase:
            job.phase_started_at = now
        if "status" in kwargs and kwargs["status"] in {JobStatus.failed, JobStatus.done, JobStatus.cancelled}:
            job.phase_progress = 100.0 if kwargs["status"] == JobStatus.done else job.phase_progress
        if "progress" in kwargs or "message" in kwargs:
            job.last_progress_at = now
        job.updated_at = now


def _cleanup_completed_job_inputs(file_id: str) -> None:
    with state_lock:
        uploaded = uploaded_files.get(file_id)
        if not uploaded:
            return

        has_active_job = any(
            job.file_id == file_id and job.status in {JobStatus.pending, JobStatus.translating}
            for job in translation_jobs.values()
        )
        if has_active_job:
            return

        input_path = uploaded.input_path
        uploaded_files.pop(file_id, None)

    if input_path and os.path.exists(input_path):
        try:
            os.remove(input_path)
        except OSError:
            pass

    if os.path.exists(settings.temp_dir):
        for name in os.listdir(settings.temp_dir):
            temp_path = os.path.join(settings.temp_dir, name)
            if not os.path.isfile(temp_path):
                continue
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _cleanup_job_output(job_id: str) -> None:
    output_path = os.path.join(settings.output_dir, f"{job_id}.pdf")
    if os.path.exists(output_path):
        try:
            os.remove(output_path)
        except OSError:
            pass


def _combined_progress(phase: str, phase_progress: float) -> float:
    p = max(0.0, min(100.0, phase_progress))
    if phase == "extracting":
        return p * 0.05
    if phase == "translating":
        return 5.0 + p * 0.75
    if phase == "rendering":
        return 80.0 + p * 0.18
    if phase == "saving":
        return 98.0 + p * 0.02
    if phase == "done":
        return 100.0
    return max(0.0, min(99.0, p))


def _translate_job_sync(job_id: str) -> None:
    with state_lock:
        job = translation_jobs[job_id]
        uploaded = uploaded_files[job.file_id]
        segments = _segments_for_page_range(uploaded, job.page_from, job.page_to)
        original_filename = uploaded.original_name

    input_size_bytes = 0
    output_size_bytes = 0
    selected_segments = len(segments)
    translate_started = datetime.utcnow()
    render_started = translate_started
    save_started = translate_started
    try:
        if _is_job_aborted(job_id):
            raise RuntimeError("Translation cancelled by user.")

        _set_job_state(
            job_id,
            status=JobStatus.translating,
            phase="translating",
            phase_progress=0.0,
            progress=_combined_progress("translating", 0.0),
            message=f"Starting translation for pages {job.page_from}-{job.page_to}...",
        )
        _set_job_phase(job_id, "translating", phase_progress=0.0)

        translated_texts: list[str] = [""] * len(segments)
        total = max(1, len(segments))
        total_input_tokens = 0
        total_output_tokens = 0
        needs_translation_count = 0

        chunks = _build_translation_chunks(segments, job.model)
        if chunks:
            worker_count = max(1, min(settings.translation_concurrency, len(chunks)))
            queue_limit = max(worker_count, worker_count * 2)
            completed_segments = 0

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                chunk_iter = iter(chunks)
                futures: dict[Future, tuple[int, int, int, list[str]]] = {}

                def _submit_next() -> bool:
                    try:
                        chunk_meta = next(chunk_iter)
                    except StopIteration:
                        return False
                    chunk_index, start_idx, end_idx, chunk_texts = chunk_meta
                    future = executor.submit(
                        _translate_chunk_worker,
                        job.model,
                        chunk_texts,
                        settings.translation_target_language,
                    )
                    futures[future] = (chunk_index, start_idx, end_idx, chunk_texts)
                    return True

                while len(futures) < queue_limit and _submit_next():
                    pass

                while futures:
                    done, _ = wait(set(futures.keys()), return_when=FIRST_COMPLETED)
                    for future in done:
                        if _is_job_aborted(job_id):
                            for pending_future in futures:
                                pending_future.cancel()
                            raise RuntimeError("Translation cancelled by user.")

                        chunk_index, start_idx, end_idx, chunk_texts = futures.pop(future)
                        try:
                            translated_batch, prompt_tokens, completion_tokens = future.result()
                        except Exception as exc:
                            for pending_future in futures:
                                pending_future.cancel()
                            raise RuntimeError(
                                "Translation chunk failed "
                                f"({chunk_index + 1}/{len(chunks)}, segments {start_idx + 1}-{end_idx}): {exc}"
                            ) from exc

                        translated_texts[start_idx:end_idx] = translated_batch
                        total_input_tokens += prompt_tokens
                        total_output_tokens += completion_tokens
                        needs_translation_count += sum(
                            1
                            for source, translated in zip(chunk_texts, translated_batch)
                            if _likely_needs_translation(source, translated)
                        )

                        completed_segments += end_idx - start_idx
                        phase_progress = min(100.0, (completed_segments / total) * 100)
                        progress = _combined_progress("translating", phase_progress)
                        _set_job_state(
                            job_id,
                            progress=progress,
                            phase="translating",
                            phase_progress=phase_progress,
                            message=(
                                f"Translated {completed_segments} / {len(segments)} lines "
                                f"(workers {worker_count})..."
                            ),
                            actual_input_tokens=total_input_tokens,
                            actual_output_tokens=total_output_tokens,
                            actual_cost_usd=calculate_cost_usd(job.model, total_input_tokens, total_output_tokens),
                        )
                        _touch_job_heartbeat(job_id)

                    while len(futures) < queue_limit and _submit_next():
                        pass

        translate_duration_seconds = max(0.0, (datetime.utcnow() - translate_started).total_seconds())

        if len(translated_texts) != len(segments):
            raise RuntimeError(
                "Translated lines do not match source lines. "
                f"Expected {len(segments)}, got {len(translated_texts)}."
            )

        high_risk_ratio = needs_translation_count / max(1, len(segments))
        if high_risk_ratio > 0.45:
            raise RuntimeError(
                "Translation quality check failed: too many lines appear untranslated. "
                f"Untranslated ratio={high_risk_ratio:.2%}."
            )

        if not uploaded.input_path or not os.path.exists(uploaded.input_path):
            raise RuntimeError("Source upload no longer exists. Please upload the PDF again.")

        output_path = os.path.join(settings.output_dir, f"{job_id}.pdf")
        render_started = datetime.utcnow()
        _set_job_phase(job_id, "rendering", phase_progress=0.0, message="Rendering translated PDF...")
        _set_job_state(
            job_id,
            phase="rendering",
            phase_progress=0.0,
            progress=_combined_progress("rendering", 0.0),
            message="Rendering translated PDF...",
            translate_duration_seconds=translate_duration_seconds,
        )

        page_indexes = sorted({segment.page_index for segment in segments})

        def _on_page_rendered(done_pages: int, total_pages: int) -> None:
            if _is_job_aborted(job_id):
                raise RuntimeError("Translation cancelled by user.")
            phase_progress = 100.0 if total_pages <= 0 else (done_pages / total_pages) * 100.0
            done_absolute = page_indexes[min(done_pages, len(page_indexes)) - 1] + 1 if page_indexes and done_pages > 0 else job.page_from
            _set_job_state(
                job_id,
                phase="rendering",
                phase_progress=phase_progress,
                progress=_combined_progress("rendering", phase_progress),
                message=(
                    f"Rendering translated PDF... {done_pages}/{max(total_pages, 1)} pages "
                    f"(up to page {done_absolute})"
                ),
            )
            _touch_job_heartbeat(job_id)

        write_translated_pdf(
            input_pdf_path=uploaded.input_path,
            output_pdf_path=output_path,
            segments=segments,
            translated_texts=translated_texts,
            on_page_rendered=_on_page_rendered,
            should_cancel=lambda: _is_job_aborted(job_id),
        )

        render_duration_seconds = max(0.0, (datetime.utcnow() - render_started).total_seconds())
        save_started = datetime.utcnow()
        _set_job_phase(job_id, "saving", phase_progress=0.0, message="Saving final PDF...")
        _set_job_state(
            job_id,
            phase="saving",
            phase_progress=0.0,
            progress=_combined_progress("saving", 0.0),
            message="Saving final PDF...",
            render_duration_seconds=render_duration_seconds,
        )

        input_size = os.path.getsize(uploaded.input_path)
        output_size = os.path.getsize(output_path)
        input_size_bytes = input_size
        output_size_bytes = output_size
        growth_ratio = output_size / max(1, input_size)
        size_message = ""
        if growth_ratio > settings.warning_output_growth_factor:
            size_message = f" Output size warning: {growth_ratio:.2f}x input."
        if growth_ratio > settings.max_output_growth_factor:
            raise RuntimeError(
                "Output PDF grew too large after translation. "
                f"Input={input_size} bytes, output={output_size} bytes, ratio={growth_ratio:.2f}x."
            )

        if _is_job_aborted(job_id):
            raise RuntimeError("Translation cancelled by user.")

        downloaded_path, translated_filename = _resolve_downloaded_path(original_name=uploaded.original_name)
        _set_job_state(
            job_id,
            phase="saving",
            phase_progress=70.0,
            progress=_combined_progress("saving", 70.0),
            message="Finalizing download package...",
        )
        shutil.copy2(output_path, downloaded_path)
        _cleanup_job_output(job_id)

        save_duration_seconds = max(0.0, (datetime.utcnow() - save_started).total_seconds())

        _set_job_state(
            job_id,
            status=JobStatus.done,
            phase="done",
            phase_progress=100.0,
            progress=100.0,
            message=f"Done.{size_message}",
            output_path=None,
            downloaded_path=downloaded_path,
            translated_filename=translated_filename,
            actual_cost_usd=calculate_cost_usd(job.model, total_input_tokens, total_output_tokens),
            translate_duration_seconds=translate_duration_seconds,
            render_duration_seconds=render_duration_seconds,
            save_duration_seconds=save_duration_seconds,
        )
    except Exception as exc:
        with state_lock:
            current_job = translation_jobs.get(job_id)
            prefailed_by_watchdog = bool(current_job and current_job.status == JobStatus.failed and current_job.timeout_reason)
        status = JobStatus.cancelled if _is_job_cancelled(job_id) else JobStatus.failed
        retry_suggestion = None
        if status == JobStatus.failed:
            retry_suggestion = _resolve_retry_page(job)
        _set_job_state(
            job_id,
            status=status,
            message=(
                "Translation cancelled by user."
                if status == JobStatus.cancelled
                else (current_job.message if prefailed_by_watchdog and current_job else f"Translation failed: {exc}")
            ),
            timeout_reason=(current_job.timeout_reason if prefailed_by_watchdog and current_job else None),
            retry_suggestion_page_from=retry_suggestion,
        )
    finally:
        with state_lock:
            final_job = translation_jobs.get(job_id)
        if final_job:
            if input_size_bytes <= 0 and uploaded.input_path and os.path.exists(uploaded.input_path):
                try:
                    input_size_bytes = os.path.getsize(uploaded.input_path)
                except OSError:
                    input_size_bytes = 0
            if output_size_bytes <= 0 and final_job.downloaded_path and os.path.exists(final_job.downloaded_path):
                try:
                    output_size_bytes = os.path.getsize(final_job.downloaded_path)
                except OSError:
                    output_size_bytes = 0
            _record_translation_history(
                job=final_job,
                original_filename=original_filename,
                selected_segments=selected_segments,
                input_size_bytes=input_size_bytes,
                output_size_bytes=output_size_bytes,
            )
        _cleanup_job_output(job_id)
        _cleanup_completed_job_inputs(job.file_id)


@app.post("/api/translate")
async def start_translation(req: TranslateRequest) -> dict:
    _validate_model(req.model)
    if not settings.openai_api_key:
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY is missing. Set it in .env before translating.")
    if not req.accept_estimate:
        raise HTTPException(status_code=400, detail="You must accept estimate before translation.")

    with state_lock:
        uploaded = uploaded_files.get(req.file_id)
    if not uploaded:
        raise HTTPException(status_code=404, detail="file_id not found")
    if not uploaded.input_path or not os.path.exists(uploaded.input_path):
        raise HTTPException(status_code=400, detail="Source file is no longer available. Please upload again.")

    page_from, page_to = _resolve_page_range(uploaded, req.page_from, req.page_to)
    selected_segments = _segments_for_page_range(uploaded, page_from, page_to)
    source_tokens = _source_tokens_for_segments(req.model, selected_segments)
    estimate = estimate_from_source_tokens(req.model, source_tokens)
    if abs(estimate.estimated_cost_usd - req.accepted_estimate_usd) > 0.0005:
        raise HTTPException(
            status_code=400,
            detail=(
                "Accepted estimate does not match latest estimate. "
                f"Current estimate is {estimate.estimated_cost_usd:.6f} USD."
            ),
        )

    job_id = str(uuid.uuid4())
    job = TranslationJob(
        job_id=job_id,
        file_id=req.file_id,
        model=req.model,
        accepted_estimate_usd=req.accepted_estimate_usd,
        page_from=page_from,
        page_to=page_to,
        status=JobStatus.pending,
        estimated_cost_usd=estimate.estimated_cost_usd,
        extraction_mode=uploaded.extraction_mode,
        ocr_preset=uploaded.ocr_preset,
        selected_segments=len(selected_segments),
        phase="pending",
    )
    with state_lock:
        translation_jobs[job_id] = job

    asyncio.create_task(asyncio.to_thread(_translate_job_sync, job_id))
    return {"job_id": job_id, "status": job.status, "phase": job.phase}


@app.get("/api/job/{job_id}")
def get_job(job_id: str) -> dict:
    with state_lock:
        job = translation_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_id not found")

    return {
        "job_id": job.job_id,
        "file_id": job.file_id,
        "model": job.model,
        "status": job.status,
        "phase": job.phase,
        "phase_progress": round(job.phase_progress, 2),
        "progress": round(job.progress, 2),
        "message": job.message,
        "estimated_cost_usd": round(job.estimated_cost_usd, 6),
        "accepted_estimate_usd": round(job.accepted_estimate_usd, 6),
        "actual_input_tokens": job.actual_input_tokens,
        "actual_output_tokens": job.actual_output_tokens,
        "actual_cost_usd": round(job.actual_cost_usd, 6),
        "selected_segments": job.selected_segments,
        "extraction_mode": job.extraction_mode,
        "ocr_preset": job.ocr_preset,
        "translate_duration_seconds": round(job.translate_duration_seconds, 2),
        "render_duration_seconds": round(job.render_duration_seconds, 2),
        "save_duration_seconds": round(job.save_duration_seconds, 2),
        "cancelled": bool(job.cancelled),
        "retry_suggestion_page_from": job.retry_suggestion_page_from,
        "timeout_reason": job.timeout_reason,
        "page_from": job.page_from,
        "page_to": job.page_to,
        "download_ready": bool(
            (job.downloaded_path and os.path.exists(job.downloaded_path))
            or (job.output_path and os.path.exists(job.output_path))
        ),
        "downloaded_ready": bool(job.downloaded_path and os.path.exists(job.downloaded_path)),
        "translated_filename": job.translated_filename,
    }


@app.post("/api/job/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    with state_lock:
        job = translation_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")
        if job.status in {JobStatus.done, JobStatus.failed, JobStatus.cancelled}:
            return {"job_id": job_id, "status": job.status, "message": "Job already finished."}

        job.cancelled = True
        job.status = JobStatus.cancelled
        job.phase = "cancelled"
        job.message = "Cancellation requested. Cleaning up..."
        job.retry_suggestion_page_from = _resolve_retry_page(job)
        job.updated_at = datetime.utcnow()
    return {"job_id": job_id, "status": JobStatus.cancelled, "message": "Cancellation requested."}


@app.get("/api/download/{job_id}")
def download_result(job_id: str) -> FileResponse:
    with state_lock:
        job = translation_jobs.get(job_id)
    fallback_output_path = os.path.join(settings.output_dir, f"{job_id}.pdf")
    output_path = ""
    if job and job.downloaded_path and os.path.exists(job.downloaded_path):
        output_path = job.downloaded_path
    elif job and job.output_path and os.path.exists(job.output_path):
        output_path = job.output_path
    elif os.path.exists(fallback_output_path):
        output_path = fallback_output_path

    if not output_path or not os.path.exists(output_path):
        raise HTTPException(status_code=404, detail="Output not found")

    filename = job.translated_filename if job and job.translated_filename else f"translated_{job_id}.pdf"
    return FileResponse(path=output_path, media_type="application/pdf", filename=filename)


@app.get("/api/downloads")
def list_downloads() -> dict:
    return {"items": _list_downloaded_files()}


@app.get("/api/history")
def list_history() -> dict:
    items = _read_history_entries(limit=300)
    return {
        "summary": _history_summary(items),
        "items": items,
    }


@app.get("/api/downloaded/{filename}")
def download_saved_file(filename: str) -> FileResponse:
    safe_filename = os.path.basename(filename)
    if safe_filename != filename or not safe_filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Invalid filename")

    file_path = os.path.join(settings.downloaded_dir, safe_filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Saved file not found")

    return FileResponse(path=file_path, media_type="application/pdf", filename=safe_filename)

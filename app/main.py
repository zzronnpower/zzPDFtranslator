from __future__ import annotations

import asyncio
import os
import re
import shutil
import uuid
from datetime import datetime
from threading import Lock

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.config import MODEL_PRICING, settings
from app.models import JobStatus, TranslationJob, UploadedFile
from app.services.dev_autolog import ensure_dev_tracking_files, record_code_snapshot_if_changed
from app.services.estimator import calculate_cost_usd, estimate_from_source_tokens
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
def on_startup() -> None:
    _ensure_storage_dirs()
    _cleanup_startup_storage()
    ensure_dev_tracking_files()
    record_code_snapshot_if_changed(event="api_startup")


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
async def upload_pdf(file: UploadFile = File(...)) -> dict:
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

    uploaded = UploadedFile(
        file_id=file_id,
        original_name=file.filename,
        input_path=input_path,
        uploaded_at=datetime.utcnow(),
        page_count=page_count,
        segments=segments,
        source_tokens=source_tokens,
    )

    with state_lock:
        uploaded_files[file_id] = uploaded

    return {
        "file_id": file_id,
        "filename": file.filename,
        "pages": page_count,
        "segments": len(segments),
        "source_tokens": source_tokens,
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
        for key, value in kwargs.items():
            setattr(job, key, value)
        job.updated_at = datetime.utcnow()


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


def _translate_job_sync(job_id: str) -> None:
    with state_lock:
        job = translation_jobs[job_id]
        uploaded = uploaded_files[job.file_id]
        segments = _segments_for_page_range(uploaded, job.page_from, job.page_to)

    try:
        _set_job_state(
            job_id,
            status=JobStatus.translating,
            progress=0.0,
            message=f"Starting translation for pages {job.page_from}-{job.page_to}...",
        )
        client = get_openai_client()
        translated_texts: list[str] = []
        total = max(1, len(segments))
        total_input_tokens = 0
        total_output_tokens = 0
        needs_translation_count = 0

        offset = 0
        while offset < len(segments):
            batch_end = _next_batch_end(segments, offset, job.model)
            batch = segments[offset:batch_end]
            texts = [segment.text for segment in batch]
            translated_batch, prompt_tokens, completion_tokens = _translate_batch_with_retry(
                client=client,
                model=job.model,
                texts=texts,
                target_language=settings.translation_target_language,
            )
            translated_texts.extend(translated_batch)
            total_input_tokens += prompt_tokens
            total_output_tokens += completion_tokens
            needs_translation_count += sum(
                1 for source, translated in zip(texts, translated_batch) if _likely_needs_translation(source, translated)
            )

            translated_count = batch_end
            progress = min(99.0, (translated_count / total) * 100)
            _set_job_state(
                job_id,
                progress=progress,
                message=f"Translated {translated_count} / {len(segments)} lines...",
                actual_input_tokens=total_input_tokens,
                actual_output_tokens=total_output_tokens,
                actual_cost_usd=calculate_cost_usd(job.model, total_input_tokens, total_output_tokens),
            )
            offset = batch_end

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
        write_translated_pdf(
            input_pdf_path=uploaded.input_path,
            output_pdf_path=output_path,
            segments=segments,
            translated_texts=translated_texts,
        )

        input_size = os.path.getsize(uploaded.input_path)
        output_size = os.path.getsize(output_path)
        growth_ratio = output_size / max(1, input_size)
        size_message = ""
        if growth_ratio > settings.warning_output_growth_factor:
            size_message = f" Output size warning: {growth_ratio:.2f}x input."
        if growth_ratio > settings.max_output_growth_factor:
            raise RuntimeError(
                "Output PDF grew too large after translation. "
                f"Input={input_size} bytes, output={output_size} bytes, ratio={growth_ratio:.2f}x."
            )

        downloaded_path, translated_filename = _resolve_downloaded_path(original_name=uploaded.original_name)
        shutil.copy2(output_path, downloaded_path)
        _cleanup_job_output(job_id)

        _set_job_state(
            job_id,
            status=JobStatus.done,
            progress=100.0,
            message=f"Done.{size_message}",
            output_path=None,
            downloaded_path=downloaded_path,
            translated_filename=translated_filename,
            actual_cost_usd=calculate_cost_usd(job.model, total_input_tokens, total_output_tokens),
        )
    except Exception as exc:
        _set_job_state(job_id, status=JobStatus.failed, message=f"Translation failed: {exc}")
    finally:
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
    )
    with state_lock:
        translation_jobs[job_id] = job

    asyncio.create_task(asyncio.to_thread(_translate_job_sync, job_id))
    return {"job_id": job_id, "status": job.status}


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
        "progress": round(job.progress, 2),
        "message": job.message,
        "estimated_cost_usd": round(job.estimated_cost_usd, 6),
        "accepted_estimate_usd": round(job.accepted_estimate_usd, 6),
        "actual_input_tokens": job.actual_input_tokens,
        "actual_output_tokens": job.actual_output_tokens,
        "actual_cost_usd": round(job.actual_cost_usd, 6),
        "page_from": job.page_from,
        "page_to": job.page_to,
        "download_ready": bool(
            (job.downloaded_path and os.path.exists(job.downloaded_path))
            or (job.output_path and os.path.exists(job.output_path))
        ),
        "downloaded_ready": bool(job.downloaded_path and os.path.exists(job.downloaded_path)),
        "translated_filename": job.translated_filename,
    }


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


@app.get("/api/downloaded/{filename}")
def download_saved_file(filename: str) -> FileResponse:
    safe_filename = os.path.basename(filename)
    if safe_filename != filename or not safe_filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Invalid filename")

    file_path = os.path.join(settings.downloaded_dir, safe_filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Saved file not found")

    return FileResponse(path=file_path, media_type="application/pdf", filename=safe_filename)

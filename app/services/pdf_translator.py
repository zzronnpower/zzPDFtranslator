from __future__ import annotations

import html
from collections.abc import Iterable
from collections.abc import Callable

import fitz
import tiktoken

from app.models import TextSegment


def _encoding_for_model(model: str):
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return tiktoken.get_encoding("cl100k_base")


def count_tokens(model: str, text: str) -> int:
    encoding = _encoding_for_model(model)
    return len(encoding.encode(text))


def extract_text_segments(pdf_path: str) -> tuple[int, list[TextSegment], int]:
    doc = fitz.open(pdf_path)
    segments: list[TextSegment] = []

    for page_index in range(len(doc)):
        page = doc[page_index]
        page_dict = page.get_text("dict")
        for block in page_dict.get("blocks", []):
            if block.get("type") != 0:
                continue

            lines: list[str] = []
            font_sizes: list[float] = []
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                line_text = "".join(span.get("text", "") for span in spans).strip()
                if not line_text:
                    continue
                lines.append(line_text)
                font_sizes.extend(float(span.get("size", 10.0)) for span in spans)

            if not lines:
                continue

            block_text = "\n".join(lines).strip()
            if not block_text:
                continue

            block_bbox = block.get("bbox")
            if not block_bbox:
                continue
            rect = fitz.Rect(block_bbox)
            if rect.is_empty or rect.width < 1 or rect.height < 1:
                continue

            avg_font_size = sum(font_sizes) / max(1, len(font_sizes))
            segment = TextSegment(
                page_index=page_index,
                rect=(rect.x0, rect.y0, rect.x1, rect.y1),
                text=block_text,
                font_size=max(8.0, avg_font_size),
            )
            segments.append(segment)

    # Estimate source tokens from extracted text in model-agnostic way.
    joined_text = "\n".join(segment.text for segment in segments)
    source_tokens = count_tokens("gpt-4o-mini", joined_text)
    page_count = len(doc)
    doc.close()
    return page_count, segments, source_tokens


def _fit_font_size(base_font_size: float, text_len: int) -> float:
    if text_len <= 60:
        return base_font_size
    if text_len <= 120:
        return max(7.5, base_font_size * 0.94)
    if text_len <= 240:
        return max(7.0, base_font_size * 0.88)
    return max(6.5, base_font_size * 0.80)


def write_translated_pdf(
    input_pdf_path: str,
    output_pdf_path: str,
    segments: list[TextSegment],
    translated_texts: Iterable[str],
    on_page_rendered: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> None:
    doc = fitz.open(input_pdf_path)

    pairs = list(zip(segments, translated_texts))
    page_items: dict[int, list[tuple[TextSegment, str]]] = {}
    for segment, translated in pairs:
        page_items.setdefault(segment.page_index, []).append((segment, translated))

    page_indices = sorted(page_items.keys())
    total_pages = len(page_indices)

    for rendered_index, page_index in enumerate(page_indices, start=1):
        if should_cancel and should_cancel():
            raise RuntimeError("Translation cancelled by user.")

        try:
            page = doc[page_index]
            for segment, translated in page_items[page_index]:
                rect = fitz.Rect(segment.rect)
                if rect.is_empty or rect.width < 1 or rect.height < 1:
                    continue

                page.draw_rect(rect, color=(1, 1, 1), fill=(1, 1, 1), overlay=True)
                candidate_font_size = _fit_font_size(segment.font_size, len(translated))
                safe_text = html.escape(translated.strip())
                inserted = False

                for _ in range(6):
                    html_block = (
                        f"<div style='font-family: DejaVu Sans; font-size: {candidate_font_size:.2f}pt; line-height: 1.15;'>"
                        f"{safe_text}</div>"
                    )
                    spare_height = page.insert_htmlbox(rect, html_block)
                    if isinstance(spare_height, tuple):
                        spare_height = float(spare_height[0])
                    if spare_height >= -0.1:
                        inserted = True
                        break
                    candidate_font_size = max(6.0, candidate_font_size * 0.92)

                if not inserted:
                    fallback_html = (
                        "<div style='font-family: DejaVu Sans; font-size: 6pt; line-height: 1.05;'>"
                        f"{safe_text}</div>"
                    )
                    page.insert_htmlbox(rect, fallback_html)
        except Exception as exc:
            raise RuntimeError(f"Render failed on page {page_index + 1}: {exc}") from exc

        if on_page_rendered:
            on_page_rendered(rendered_index, total_pages)

    try:
        doc.save(
            output_pdf_path,
            garbage=4,
            clean=True,
            deflate=True,
            deflate_images=True,
            deflate_fonts=True,
            incremental=False,
            use_objstms=1,
        )
    except TypeError:
        doc.save(output_pdf_path, garbage=4, clean=True, deflate=True)
    doc.close()

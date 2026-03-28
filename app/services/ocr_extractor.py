from __future__ import annotations

from collections import defaultdict

import fitz
from PIL import Image
import pytesseract

from app.models import TextSegment
from app.services.pdf_translator import count_tokens


def _line_key(block_num: int, par_num: int, line_num: int) -> tuple[int, int, int]:
    return block_num, par_num, line_num


def extract_text_segments_with_ocr(
    pdf_path: str,
    *,
    dpi: int = 220,
    lang: str = "eng",
    min_confidence: int = 25,
) -> tuple[int, list[TextSegment], int, int]:
    doc = fitz.open(pdf_path)
    segments: list[TextSegment] = []
    ocr_pages = 0

    for page_index in range(len(doc)):
        page = doc[page_index]
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT, lang=lang)
        if not data:
            continue

        lines: dict[tuple[int, int, int], dict] = defaultdict(
            lambda: {
                "words": [],
                "left": float("inf"),
                "top": float("inf"),
                "right": 0.0,
                "bottom": 0.0,
            }
        )

        count = len(data.get("text", []))
        for i in range(count):
            text = str(data["text"][i]).strip()
            if not text:
                continue

            raw_conf = str(data.get("conf", [""] * count)[i]).strip()
            try:
                confidence = int(float(raw_conf))
            except ValueError:
                confidence = -1
            if confidence >= 0 and confidence < min_confidence:
                continue

            block_num = int(data.get("block_num", [0] * count)[i])
            par_num = int(data.get("par_num", [0] * count)[i])
            line_num = int(data.get("line_num", [0] * count)[i])
            key = _line_key(block_num, par_num, line_num)
            row = lines[key]

            left = float(data.get("left", [0] * count)[i])
            top = float(data.get("top", [0] * count)[i])
            width = float(data.get("width", [0] * count)[i])
            height = float(data.get("height", [0] * count)[i])

            row["words"].append(text)
            row["left"] = min(row["left"], left)
            row["top"] = min(row["top"], top)
            row["right"] = max(row["right"], left + width)
            row["bottom"] = max(row["bottom"], top + height)

        scale_x = page.rect.width / max(1, pix.width)
        scale_y = page.rect.height / max(1, pix.height)
        page_added = 0

        for key in sorted(lines.keys()):
            row = lines[key]
            if not row["words"]:
                continue
            line_text = " ".join(row["words"]).strip()
            if not line_text:
                continue

            x0 = row["left"] * scale_x
            y0 = row["top"] * scale_y
            x1 = row["right"] * scale_x
            y1 = row["bottom"] * scale_y
            rect = fitz.Rect(x0, y0, x1, y1)
            if rect.is_empty or rect.width < 1 or rect.height < 1:
                continue

            approx_font_size = max(8.0, rect.height * 0.75)
            segments.append(
                TextSegment(
                    page_index=page_index,
                    rect=(rect.x0, rect.y0, rect.x1, rect.y1),
                    text=line_text,
                    font_size=approx_font_size,
                )
            )
            page_added += 1

        if page_added > 0:
            ocr_pages += 1

    joined_text = "\n".join(segment.text for segment in segments)
    source_tokens = count_tokens("gpt-4o-mini", joined_text)
    page_count = len(doc)
    doc.close()
    return page_count, segments, source_tokens, ocr_pages

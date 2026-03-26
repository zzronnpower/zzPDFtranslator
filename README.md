# zzPDFtranslator

![Python](https://img.shields.io/badge/python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115.0-009688)
![License](https://img.shields.io/badge/license-MIT-green)
![Docker](https://img.shields.io/badge/docker-ready-2496ED)

Web app flow:

1. Upload a PDF
2. Choose translation model
3. Optionally choose a page range (for lower-cost preview)
4. Estimate cost in USD
5. Confirm "Yes, accept estimate"
6. Translate and download translated PDF

## Tech Stack

- FastAPI backend
- Vanilla HTML/CSS/JS frontend
- PyMuPDF for PDF text extraction and writing
- OpenAI API for translation
- Token-based cost estimation with model pricing table

## Project Structure

```text
zzPDFtranslator/
  app/
    main.py
    config.py
    models.py
    services/
      estimator.py
      openai_translator.py
      pdf_translator.py
    web/
      index.html
      app.js
      styles.css
  storage/
    input/
    output/
    tmp/
  requirements.txt
  .env.example
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `OPENAI_API_KEY` in `.env`.
If the key is missing, estimate still works, but translation is blocked with a clear error.

## Run

```bash
source .venv/bin/activate
set -a; source .env; set +a
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open: `http://localhost:8000`

## Docker Quick Deploy

```bash
cp .env.example .env
# edit .env and set OPENAI_API_KEY
docker compose up -d --build
```

Open: `http://localhost:8000`

Stop:

```bash
docker compose down
```

## Environment

- `OPENAI_API_KEY`: required for translation
- `DEFAULT_MODEL`: default `gpt-4o-mini`
- `TRANSLATION_TARGET_LANGUAGE`: default `Vietnamese`
- `TRANSLATION_BATCH_MAX_ITEMS`: adaptive batch max item count
- `TRANSLATION_BATCH_MAX_TOKENS`: adaptive batch token budget
- `WARNING_OUTPUT_GROWTH_FACTOR`: warning threshold for output/input ratio
- `MAX_OUTPUT_GROWTH_FACTOR`: fail threshold for output/input ratio

## Notes

- The model pricing table is in `app/config.py` (`MODEL_PRICING`).
- Cost estimate is computed from extracted source tokens and estimated output tokens.
- You can estimate and translate only selected pages (for example, pages 1-5 first).
- Translation starts only after explicit estimate acceptance.
- Final API usage cost is tracked and shown after/during translation.
- Layout is preserved by rewriting translated text into original text bounding boxes. Complex PDFs can still need manual QA.
- Saved output names use `Translated_<original_name>.pdf` (with `_v2`, `_v3` if duplicated).
- The app now performs translation completeness checks and fails early if too many lines appear untranslated.
- Text extraction and rewrite now run on text blocks (paragraph-level) to reduce render operations and output size growth.
- Output size safeguards: warning threshold (`WARNING_OUTPUT_GROWTH_FACTOR`) and hard-fail threshold (`MAX_OUTPUT_GROWTH_FACTOR`).
- Translation batching is adaptive by token budget (`TRANSLATION_BATCH_MAX_ITEMS`, `TRANSLATION_BATCH_MAX_TOKENS`) with fallback split-and-retry for missing item IDs.
- API startup creates and updates `agents/`, `chatlog/`, and `logs/code_changes.log` when code state changes.

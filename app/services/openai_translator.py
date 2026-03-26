from __future__ import annotations

import json

from openai import OpenAI

from app.config import settings


TRANSLATION_SYSTEM_PROMPT = (
    "You are a precise technical translator. Translate English text to Vietnamese with strict fidelity. "
    "Do not add explanations, examples, or commentary. Keep line intent, punctuation, and formatting style. "
    "Keep international abbreviations and acronyms unchanged (e.g., LED, AI, CPU), keep units, codes, "
    "and product identifiers exactly as in source."
)


def get_openai_client() -> OpenAI:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is missing. Set it in your environment.")
    return OpenAI(api_key=settings.openai_api_key)


def translate_batch(
    client: OpenAI,
    model: str,
    texts: list[str],
    target_language: str,
) -> tuple[list[str], int, int, list[int]]:
    payload = {"target_language": target_language, "items": [{"id": i, "text": text} for i, text in enumerate(texts)]}

    response = client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": TRANSLATION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Translate each item text faithfully. Return strict JSON with this shape: "
                    "{\"items\":[{\"id\": number, \"translated\": string}]}.\n"
                    f"Input:\n{json.dumps(payload, ensure_ascii=False)}"
                ),
            },
        ],
    )

    content = response.choices[0].message.content or "{}"
    data = json.loads(content)
    rows = data.get("items", [])
    id_to_text = {int(row["id"]): str(row.get("translated", "")) for row in rows if "id" in row}

    missing_ids = [i for i in range(len(texts)) if i not in id_to_text]
    translated = [id_to_text.get(i, texts[i]) for i in range(len(texts))]
    prompt_tokens = int(response.usage.prompt_tokens if response.usage else 0)
    completion_tokens = int(response.usage.completion_tokens if response.usage else 0)
    return translated, prompt_tokens, completion_tokens, missing_ids

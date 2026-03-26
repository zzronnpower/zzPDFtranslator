from __future__ import annotations

import os
from dataclasses import dataclass


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_dotenv()


@dataclass(frozen=True)
class ModelPricing:
    input_per_million: float
    output_per_million: float


# Pricing is configurable here. Update if OpenAI changes prices.
MODEL_PRICING: dict[str, ModelPricing] = {
    "gpt-4o-mini": ModelPricing(input_per_million=0.15, output_per_million=0.60),
    "gpt-4.1-mini": ModelPricing(input_per_million=0.40, output_per_million=1.60),
    "gpt-4o": ModelPricing(input_per_million=2.50, output_per_million=10.00),
    "gpt-4.1": ModelPricing(input_per_million=2.00, output_per_million=8.00),
}


class Settings:
    app_name: str = "zzPDFtranslator"
    project_root: str = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    storage_root: str = os.path.join(project_root, "storage")
    input_dir: str = os.path.join(storage_root, "input")
    output_dir: str = os.path.join(storage_root, "output")
    downloaded_dir: str = os.path.join(storage_root, "downloaded")
    temp_dir: str = os.path.join(storage_root, "tmp")
    logs_dir: str = os.path.join(project_root, "logs")
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")
    default_model: str = os.getenv("DEFAULT_MODEL", "gpt-4o-mini")
    translation_target_language: str = os.getenv("TRANSLATION_TARGET_LANGUAGE", "Vietnamese")
    output_token_factor: float = float(os.getenv("OUTPUT_TOKEN_FACTOR", "1.18"))
    translation_batch_max_items: int = int(os.getenv("TRANSLATION_BATCH_MAX_ITEMS", "16"))
    translation_batch_max_tokens: int = int(os.getenv("TRANSLATION_BATCH_MAX_TOKENS", "3500"))
    warning_output_growth_factor: float = float(os.getenv("WARNING_OUTPUT_GROWTH_FACTOR", "4.0"))
    max_output_growth_factor: float = float(os.getenv("MAX_OUTPUT_GROWTH_FACTOR", "8.0"))


settings = Settings()

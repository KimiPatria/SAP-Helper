"""Provider selection - the single place that knows which backends exist."""

from __future__ import annotations

from app.config import Settings
from app.generation.base import GenerationError, GenerationProvider


def build_provider(settings: Settings) -> GenerationProvider:
    name = settings.generation_provider.strip().lower()
    if name == "groq":
        from app.generation.groq_provider import GroqProvider

        return GroqProvider(api_key=settings.groq_api_key, model=settings.groq_model)
    if name == "bedrock":
        from app.generation.bedrock import BedrockProvider

        return BedrockProvider(
            aws_region=settings.aws_region,
            model_id=settings.bedrock_model_id,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
    raise GenerationError(
        f"Unknown GENERATION_PROVIDER '{settings.generation_provider}'. "
        "Supported values: 'groq', 'bedrock'."
    )

"""Groq adapter (default model: llama-3.3-70b-versatile).

All Groq-specific code stays inside this file.
"""

from __future__ import annotations

from app.generation.base import GenerationError, GenerationProvider, GenerationResult


class GroqProvider(GenerationProvider):
    name = "groq"

    def __init__(self, api_key: str, model: str):
        import groq  # deferred: only loaded when this provider is selected

        if not api_key:
            raise GenerationError(
                "GROQ_API_KEY is not set. Add it to .env or the environment, "
                "or switch GENERATION_PROVIDER to 'bedrock'."
            )
        self.model = model
        self._groq = groq
        self._client = groq.Groq(api_key=api_key)

    def generate(self, system_prompt: str, user_prompt: str, max_tokens: int) -> GenerationResult:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=0.2,  # grounded synthesis, not creative writing
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
        except self._groq.APIConnectionError as exc:
            raise GenerationError(f"Groq connection failed: {exc}") from exc
        except self._groq.APIStatusError as exc:
            raise GenerationError(f"Groq request failed ({exc.status_code}): {exc.message}") from exc

        answer = (response.choices[0].message.content or "").strip()
        if not answer:
            raise GenerationError("Groq returned an empty response.")
        return GenerationResult(answer=answer, provider=self.name, model=self.model)

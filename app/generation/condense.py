"""Retrieval-query condensation and translation.

A follow-up like "what about the network component?" embeds to near
nothing useful on its own. This rewrites it into a standalone English
query using the conversation so far (runs on first turns too, so
non-English questions are translated for the English-only embedder).
Only used for retrieval - the answer prompt still sees the user's
literal question.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.generation.base import GenerationProvider
from app.generation.prompts import CONDENSE_SYSTEM_PROMPT, build_condense_prompt

if TYPE_CHECKING:
    from app.models import HistoryTurn


def condense_question(
    provider: GenerationProvider,
    history: list["HistoryTurn"],
    question: str,
    max_tokens: int,
) -> str:
    result = provider.generate(
        CONDENSE_SYSTEM_PROMPT,
        build_condense_prompt(history, question),
        max_tokens,
    )
    return result.answer.strip() or question

"""Generation provider interface.

One contract for "answer a question given retrieved context". Providers
receive the already-built system prompt and user prompt (prompt assembly
is shared in prompts.py, not duplicated per provider) and return plain
text. Adding a third provider = one new subclass + one factory branch.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class GenerationError(Exception):
    """Raised when a provider cannot produce an answer (auth, quota, network)."""


@dataclass(frozen=True)
class GenerationResult:
    answer: str
    provider: str
    model: str


class GenerationProvider(ABC):
    name: str
    model: str

    @abstractmethod
    def generate(self, system_prompt: str, user_prompt: str, max_tokens: int) -> GenerationResult:
        """Produce an answer. Must raise GenerationError on provider failure."""

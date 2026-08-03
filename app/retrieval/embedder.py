"""Embedding abstraction.

Self-hosted, open-source embeddings - no external embedding API. The
default implementation runs BAAI/bge-small-en-v1.5 locally via fastembed
(ONNX runtime; the model weights download once from Hugging Face and are
cached). Swapping models (or moving to a GPU sentence-transformers
deployment) is a new subclass behind the same two methods.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class Embedder(ABC):
    model_name: str

    @property
    @abstractmethod
    def dim(self) -> int: ...

    @abstractmethod
    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        """Embed document chunks for indexing."""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """Embed a user query (some models require a query-side prefix)."""


class FastEmbedEmbedder(Embedder):
    def __init__(self, model_name: str):
        from fastembed import TextEmbedding  # deferred: heavy import

        self.model_name = model_name
        self._model = TextEmbedding(model_name=model_name)
        self._dim = len(next(iter(self._model.embed(["dimension probe"]))))

    @property
    def dim(self) -> int:
        return self._dim

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        # passage_embed applies the model's document-side conventions where defined.
        embed_fn = getattr(self._model, "passage_embed", self._model.embed)
        return [vec.tolist() for vec in embed_fn(texts)]

    def embed_query(self, text: str) -> list[float]:
        # query_embed applies the model's query instruction prefix (BGE models
        # expect one for retrieval); falls back to plain embed if unavailable.
        embed_fn = getattr(self._model, "query_embed", self._model.embed)
        return next(iter(embed_fn([text]))).tolist()


def build_embedder(model_name: str) -> Embedder:
    return FastEmbedEmbedder(model_name)

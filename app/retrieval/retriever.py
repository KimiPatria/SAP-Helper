"""Retrieval: query -> top-k chunks -> citations.

Citations are deduplicated per note (best score wins) and carry a link
built from the configurable SAP Note URL template.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.retrieval.embedder import Embedder
from app.retrieval.store import RetrievedChunk, VectorStore


class RetrievalError(Exception):
    """Raised when a knowledge base cannot be queried (auth, quota, network, misconfiguration)."""


@dataclass(frozen=True)
class Citation:
    note_id: str
    title: str
    url: str
    component: str
    source_file: str
    score: float
    sections: tuple[str, ...]


class Retriever:
    name = "local"

    def __init__(self, embedder: Embedder, store: VectorStore, url_template: str):
        self.embedder = embedder
        self.store = store
        self.url_template = url_template

    def retrieve(self, question: str, top_k: int) -> tuple[list[RetrievedChunk], list[Citation]]:
        vector = self.embedder.embed_query(question)
        chunks = self.store.search(vector, top_k)
        return chunks, self._citations(chunks)

    def _citations(self, chunks: list[RetrievedChunk]) -> list[Citation]:
        by_note: dict[str, dict] = {}
        for chunk in chunks:
            if not chunk.note_id:
                continue
            entry = by_note.setdefault(
                chunk.note_id,
                {
                    "title": chunk.title,
                    "component": chunk.component,
                    "source_file": chunk.source_file,
                    "score": chunk.score,
                    "sections": [],
                },
            )
            entry["score"] = max(entry["score"], chunk.score)
            if chunk.section not in entry["sections"]:
                entry["sections"].append(chunk.section)
        citations = [
            Citation(
                note_id=note_id,
                title=e["title"],
                url=self.url_template.format(note_id=note_id),
                component=e["component"],
                source_file=e["source_file"],
                score=round(e["score"], 4),
                sections=tuple(e["sections"]),
            )
            for note_id, e in by_note.items()
        ]
        return sorted(citations, key=lambda c: c.score, reverse=True)

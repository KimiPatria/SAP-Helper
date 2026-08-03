"""Vector store on Qdrant with scalar quantization.

Qdrant is a production-grade vector database whose int8 scalar
quantization cuts vector memory ~4x - the property that makes a
2M-document index (~10M chunks at 384 dims) feasible on modest hardware.

Two connection modes behind one code path:
  - QDRANT_URL unset  -> embedded mode, persisted to local disk (POC default;
    note that embedded mode accepts the quantization config but does not
    exercise it - the server does).
  - QDRANT_URL set    -> any Qdrant server or cluster; quantized HNSW search,
    sharding and replication all apply without touching this file.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from qdrant_client import QdrantClient
from qdrant_client import models as qm

from app.ingestion.chunker import Chunk

log = logging.getLogger(__name__)

# Namespace for deterministic point IDs: re-ingesting the same chunk of the
# same document overwrites rather than duplicates.
_POINT_NS = uuid.UUID("7d9a2c52-1f0e-4b3a-9d2f-3a5e8c1b4f60")


@dataclass(frozen=True)
class RetrievedChunk:
    text: str
    score: float
    note_id: str
    title: str
    section: str
    component: str
    source_file: str


class VectorStore:
    def __init__(
        self,
        collection: str,
        dim: int,
        url: str = "",
        api_key: str = "",
        path: str = "./data/qdrant",
    ):
        self.collection = collection
        self.dim = dim
        if url:
            self._client = QdrantClient(url=url, api_key=api_key or None)
            self.mode = "server"
        else:
            self._client = QdrantClient(path=path)
            self.mode = "embedded"
        self._ensure_collection()

    # -- lifecycle -----------------------------------------------------------

    def _ensure_collection(self) -> None:
        if self._client.collection_exists(self.collection):
            return
        self._create_collection()

    def _create_collection(self) -> None:
        vectors = qm.VectorParams(size=self.dim, distance=qm.Distance.COSINE)
        quantization = qm.ScalarQuantization(
            scalar=qm.ScalarQuantizationConfig(
                type=qm.ScalarType.INT8,
                quantile=0.99,
                always_ram=True,  # quantized vectors in RAM, originals on disk
            )
        )
        try:
            self._client.create_collection(
                collection_name=self.collection,
                vectors_config=vectors,
                quantization_config=quantization,
                # Original vectors go to memory-mapped storage; searches run on
                # the in-RAM int8 index with rescoring against originals.
                on_disk_payload=True,
            )
        except (TypeError, ValueError):
            # Defensive: if a client/server combination rejects the quantization
            # config, still come up (unquantized) rather than hard-fail the POC.
            log.warning("Quantization config rejected; creating collection without it")
            self._client.create_collection(
                collection_name=self.collection, vectors_config=vectors
            )
        # Payload index for filtered deletes/lookups by source document.
        self._client.create_payload_index(
            collection_name=self.collection,
            field_name="doc_id",
            field_schema=qm.PayloadSchemaType.KEYWORD,
        )

    def reset_collection(self) -> None:
        if self._client.collection_exists(self.collection):
            self._client.delete_collection(self.collection)
        self._create_collection()

    # -- writes ----------------------------------------------------------------

    def delete_by_source(self, doc_id: str) -> None:
        self._client.delete(
            collection_name=self.collection,
            points_selector=qm.FilterSelector(
                filter=qm.Filter(
                    must=[qm.FieldCondition(key="doc_id", match=qm.MatchValue(value=doc_id))]
                )
            ),
        )

    def upsert(self, doc_id: str, chunks: list[Chunk], vectors: list[list[float]], batch_size: int = 128) -> None:
        points = [
            qm.PointStruct(
                id=str(uuid.uuid5(_POINT_NS, f"{doc_id}::{chunk.chunk_index}")),
                vector=vector,
                payload={
                    "doc_id": doc_id,
                    "text": chunk.text,
                    "note_id": chunk.note_id,
                    "title": chunk.title,
                    "section": chunk.section,
                    "component": chunk.component,
                    "source_file": chunk.source_file,
                },
            )
            for chunk, vector in zip(chunks, vectors)
        ]
        for start in range(0, len(points), batch_size):
            self._client.upsert(
                collection_name=self.collection,
                points=points[start : start + batch_size],
            )

    # -- reads -----------------------------------------------------------------

    def search(self, vector: list[float], top_k: int) -> list[RetrievedChunk]:
        result = self._client.query_points(
            collection_name=self.collection,
            query=vector,
            limit=top_k,
            with_payload=True,
        )
        hits = []
        for point in result.points:
            p = point.payload or {}
            hits.append(
                RetrievedChunk(
                    text=p.get("text", ""),
                    score=float(point.score),
                    note_id=p.get("note_id", ""),
                    title=p.get("title", ""),
                    section=p.get("section", ""),
                    component=p.get("component", ""),
                    source_file=p.get("source_file", ""),
                )
            )
        return hits

    def count(self) -> int:
        return self._client.count(self.collection, exact=True).count

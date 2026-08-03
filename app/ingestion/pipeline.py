"""Ingestion pipeline: source -> parse -> chunk -> embed -> store.

Streams one document at a time (no whole-corpus loads) and upserts in
batches, so the same loop that handles 10 PDFs handles a large corpus -
throughput scales by parallelizing the loop, not rewriting it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import settings
from app.ingestion.chunker import chunk_note
from app.ingestion.ledger import IngestionLedger
from app.ingestion.parser import parse_note
from app.ingestion.sources import DocumentSource
from app.retrieval.embedder import Embedder
from app.retrieval.store import VectorStore

log = logging.getLogger(__name__)


@dataclass
class IngestReport:
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    chunks: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def run_ingestion(
    source: DocumentSource,
    embedder: Embedder,
    store: VectorStore,
    rebuild: bool = False,
) -> IngestReport:
    ledger = IngestionLedger(settings.ledger_path)
    if rebuild:
        ledger.clear()
        store.reset_collection()

    report = IngestReport()
    for doc in source.list_documents():
        if ledger.is_current(doc.doc_id, doc.content_hash):
            report.skipped += 1
            continue
        try:
            note = parse_note(source.read(doc), doc.display_name)
            chunks = chunk_note(note, settings.max_chunk_chars, settings.chunk_overlap_chars)
            if not chunks:
                log.warning("No text extracted from %s - skipping", doc.display_name)
                report.failed += 1
                continue
            vectors = embedder.embed_passages([c.text for c in chunks])
            # Idempotent per document: changed files replace their old points.
            store.delete_by_source(doc.doc_id)
            store.upsert(doc.doc_id, chunks, vectors)
            ledger.record(doc.doc_id, doc.content_hash, note.note_id, len(chunks))
            report.processed += 1
            report.chunks += len(chunks)
            log.info("Ingested %s (note %s, %d chunks)", doc.display_name, note.note_id, len(chunks))
        except Exception:
            log.exception("Failed to ingest %s", doc.display_name)
            report.failed += 1
    ledger.save()
    return report

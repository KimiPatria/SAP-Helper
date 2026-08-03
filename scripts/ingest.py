"""Ingest a folder of SAP Note PDFs into the vector store.

Usage:
    python -m scripts.ingest                 # incremental (skips unchanged files)
    python -m scripts.ingest --rebuild       # wipe collection + ledger, re-ingest all
    python -m scripts.ingest --notes-dir X   # override the source folder

Note: with the embedded Qdrant store (QDRANT_URL unset) the database is a
single-process file lock - stop the API server before running this, or use
POST /api/ingest on the running server instead.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.ingestion.pipeline import run_ingestion
from app.ingestion.sources import LocalFolderSource
from app.retrieval.embedder import build_embedder
from app.retrieval.store import VectorStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest SAP Note PDFs")
    parser.add_argument("--notes-dir", default=settings.notes_dir)
    parser.add_argument("--rebuild", action="store_true", help="recreate collection and ledger")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    print(f"Loading embedding model {settings.embedding_model} ...")
    embedder = build_embedder(settings.embedding_model)
    store = VectorStore(
        collection=settings.qdrant_collection,
        dim=embedder.dim,
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        path=settings.qdrant_path,
    )

    report = run_ingestion(LocalFolderSource(args.notes_dir), embedder, store, rebuild=args.rebuild)
    print(
        f"\nDone. processed={report.processed} skipped={report.skipped} "
        f"failed={report.failed} chunks={report.chunks} "
        f"(collection now holds {store.count()} chunks)"
    )
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Incremental-ingestion ledger.

Tracks which source documents have already been processed, keyed by
doc_id with the content hash of the version we ingested. Re-running
ingestion skips unchanged documents and re-processes changed ones.

A JSON file is deliberately the simplest thing that demonstrates the
pattern; at production scale this becomes a table in the metadata store,
with the same three operations.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class IngestionLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._entries: dict[str, dict] = {}
        if self.path.exists():
            self._entries = json.loads(self.path.read_text(encoding="utf-8"))

    def is_current(self, doc_id: str, content_hash: str) -> bool:
        entry = self._entries.get(doc_id)
        return bool(entry) and entry.get("content_hash") == content_hash

    def record(self, doc_id: str, content_hash: str, note_id: str, chunk_count: int) -> None:
        self._entries[doc_id] = {
            "content_hash": content_hash,
            "note_id": note_id,
            "chunk_count": chunk_count,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }

    def clear(self) -> None:
        self._entries = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._entries, indent=2), encoding="utf-8")

    def __len__(self) -> int:
        return len(self._entries)

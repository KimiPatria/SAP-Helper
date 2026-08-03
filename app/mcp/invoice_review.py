"""Human review queue - where the pipeline parks invoices it must not guess on.

When a critical field (PO number, item, quantity, price) comes back below the
confidence threshold, or a PO line can't be pinned down, the pipeline does NOT
proceed on a best guess: it enqueues the case here for a person to handle. A
JSON-backed append log, same durable pattern as the other ledgers - a table in
production, keeping the same enqueue/list operations.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


class ReviewQueue:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._entries: list[dict] = []
        if self.path.exists():
            self._entries = json.loads(self.path.read_text(encoding="utf-8"))

    def enqueue(self, item: dict) -> dict:
        entry = {
            "id": uuid.uuid4().hex,
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "status": "open",
            **item,
        }
        self._entries.append(entry)
        self._save()
        return entry

    def list_open(self) -> list[dict]:
        return [e for e in self._entries if e.get("status") == "open"]

    def all(self) -> list[dict]:
        return list(self._entries)

    def get(self, review_id: str) -> dict | None:
        return next((e for e in self._entries if e.get("id") == review_id), None)

    def resolve(self, review_id: str, **resolution: object) -> dict | None:
        """Close an open entry with how it was resolved (e.g. a human-corrected
        PO number). Returns None if the id is unknown or already resolved -
        the caller decides what that means (retry_match_with_corrected_po
        treats it as "nothing to retry")."""
        entry = self.get(review_id)
        if entry is None or entry.get("status") != "open":
            return None
        entry["status"] = "resolved"
        entry["resolved_at"] = datetime.now(timezone.utc).isoformat()
        entry.update(resolution)
        self._save()
        return entry

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._entries, indent=2), encoding="utf-8")

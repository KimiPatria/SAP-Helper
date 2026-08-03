"""Idempotency ledger for executed creates.

Same pattern as the ingestion ledger (app/ingestion/ledger.py): a JSON
file as the simplest thing that demonstrates the mechanism; at production
scale this becomes a table with the same operations.

Keyed on sha256(payload_hash + request_id) where request_id is the
preview_id - so a retried/duplicated create call for the *same confirmed
preview* returns the PO SAP already assigned instead of POSTing again,
including across server restarts (which is exactly when blind retries
happen). A genuinely new order for identical line items gets a new
preview_id and therefore a new key; the preview layer warns about the
recent twin instead of this layer refusing it.

Entries are recorded only after SAP returns a document number. Note the
POC gap this leaves: if the POST times out after SAP committed, nothing
was recorded - see README ("known limits").
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


class IdempotencyRegistry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._entries: dict[str, dict] = {}
        if self.path.exists():
            self._entries = json.loads(self.path.read_text(encoding="utf-8"))

    @staticmethod
    def key(payload_hash: str, request_id: str) -> str:
        return hashlib.sha256(f"{payload_hash}:{request_id}".encode()).hexdigest()

    def find(self, key: str) -> dict | None:
        return self._entries.get(key)

    def find_by_request(self, request_id: str) -> dict | None:
        """Execution record for a preview id, regardless of key - answers a
        retried create whose preview is already consumed or gone (restart)."""
        for entry in self._entries.values():
            if entry.get("request_id") == request_id:
                return entry
        return None

    def find_by_payload(self, payload_hash: str) -> dict | None:
        """Most recent execution of an identical payload (any request_id) -
        feeds the preview-layer 'possible duplicate' warning."""
        matches = [e for e in self._entries.values()
                   if e.get("payload_hash") == payload_hash]
        return max(matches, key=lambda e: e.get("created_at", "")) if matches else None

    def record(self, key: str, payload_hash: str, request_id: str,
               purchase_order: str) -> None:
        self._entries[key] = {
            "payload_hash": payload_hash,
            "request_id": request_id,
            "purchase_order": purchase_order,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._entries, indent=2), encoding="utf-8")

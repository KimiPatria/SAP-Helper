"""Single-use preview store - the write-safety boundary.

`preview_purchase_order` deposits the fully-resolved payload here and
returns only its `preview_id`. `create_purchase_order` accepts *nothing
but* a preview_id, so there is no code path where the model hands the
create tool a payload it invented: if it wasn't minted by the preview
step of this server process, the create tool cannot see it.

Entries are deliberately in-memory only:
* they are proposals, not records - nothing worth persisting exists
  until SAP assigns a document number (that goes in the idempotency
  ledger, registry.py);
* a server restart invalidating pending previews is the safe failure
  mode - the user re-runs preview and re-confirms against fresh data.

Entries expire after a TTL and are consumed exactly once on successful
creation. A failed create leaves the preview claimable so the user can
retry without re-confirming an unchanged proposal.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class PreviewEntry:
    preview_id: str
    payload: dict
    payload_hash: str
    summary: str
    created_at: float = field(default_factory=time.monotonic)
    consumed: bool = False


class PreviewStore:
    def __init__(self, ttl_seconds: int):
        self._ttl = ttl_seconds
        self._entries: dict[str, PreviewEntry] = {}

    def put(self, payload: dict, payload_hash: str, summary: str) -> PreviewEntry:
        self._prune()
        entry = PreviewEntry(
            preview_id=uuid.uuid4().hex,
            payload=payload,
            payload_hash=payload_hash,
            summary=summary,
        )
        self._entries[entry.preview_id] = entry
        return entry

    def claim(self, preview_id: str) -> tuple[PreviewEntry | None, str]:
        """Look up a preview for execution. Returns (entry, "") on success
        or (None, reason) with an agent-actionable reason otherwise."""
        entry = self._entries.get(preview_id)
        if entry is None:
            return None, (
                "Unknown preview_id. It may have expired, or the server was "
                "restarted since the preview was made. Call "
                "preview_purchase_order again, show the new preview to the "
                "user, and get fresh confirmation before creating."
            )
        if entry.consumed:
            return None, (
                "This preview was already executed - the purchase order was "
                "created. Use get_purchase_order_status to look it up instead "
                "of creating it again."
            )
        if time.monotonic() - entry.created_at > self._ttl:
            del self._entries[preview_id]
            return None, (
                "This preview has expired (previews are valid for "
                f"{self._ttl // 60} minutes). Prices and master data may have "
                "changed - call preview_purchase_order again and re-confirm "
                "with the user."
            )
        return entry, ""

    def mark_consumed(self, preview_id: str) -> None:
        entry = self._entries.get(preview_id)
        if entry:
            entry.consumed = True

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [k for k, e in self._entries.items()
                    if now - e.created_at > self._ttl]:
            del self._entries[key]

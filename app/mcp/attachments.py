"""In-memory store for files a user attaches to a chat turn.

Same mechanism and rationale as `previews.py` and `invoice_proposals.py`: the
upload endpoint deposits the bytes here and hands back only an id, so the id is
the only thing that travels through the chat payload and the LLM's tool call.
The model never carries file bytes, and it cannot fabricate an attachment - an
unknown id simply resolves to nothing.

Entries expire after a TTL and the store is capped, because an attachment is
only interesting while the turn that referenced it is in flight. Nothing is
written to disk: an invoice image is the user's document, not ours to persist,
and the durable records of what happened to it are the ledgers.

Thread-safe: FastAPI runs the upload and the chat turn in different threadpool
workers.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

# Generous for a chat turn, short enough that a forgotten upload does not sit
# in memory all day.
_DEFAULT_TTL_SECONDS = 1800
# Hard ceiling on retained attachments, oldest evicted first.
_MAX_ENTRIES = 40


@dataclass
class Attachment:
    attachment_id: str
    filename: str
    content_type: str
    data: bytes
    created_at: float = field(default_factory=time.monotonic)

    @property
    def size(self) -> int:
        return len(self.data)


class AttachmentStore:
    def __init__(self, ttl_seconds: int = _DEFAULT_TTL_SECONDS, max_entries: int = _MAX_ENTRIES):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._entries: OrderedDict[str, Attachment] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, filename: str, content_type: str, data: bytes) -> Attachment:
        entry = Attachment(
            attachment_id=uuid.uuid4().hex,
            filename=filename or "attachment",
            content_type=content_type or "",
            data=data,
        )
        with self._lock:
            self._prune()
            self._entries[entry.attachment_id] = entry
            self._entries.move_to_end(entry.attachment_id)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)
        return entry

    def get(self, attachment_id: str) -> Attachment | None:
        """The attachment, or None if the id is unknown or expired. Callers turn
        None into an actionable "re-attach the file" message rather than raising -
        it reaches the model as a tool result it can recover from."""
        with self._lock:
            entry = self._entries.get(attachment_id)
            if entry is None:
                return None
            if time.monotonic() - entry.created_at > self._ttl:
                del self._entries[attachment_id]
                return None
            return entry

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [k for k, e in self._entries.items() if now - e.created_at > self._ttl]:
            del self._entries[key]


# One process-wide store; the upload endpoint and the tool layer share it.
attachment_store = AttachmentStore()

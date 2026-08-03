"""Document source abstraction.

Ingestion and retrieval never touch file paths directly - they consume
`DocumentSource`. The POC ships `LocalFolderSource` (a folder of PDFs);
swapping in a client-portal API later means writing one new subclass that
yields the same `SourceDocument` records, with no change to parsing,
chunking, embedding, or storage.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class SourceDocument:
    """A handle to one raw document, independent of where it lives."""

    doc_id: str        # stable identifier within the source (e.g. relative path)
    display_name: str  # human-readable name for citations/logs
    content_hash: str  # sha256 of the raw bytes - drives incremental ingestion


class DocumentSource(ABC):
    """Anything that can enumerate documents and hand back their bytes."""

    @abstractmethod
    def list_documents(self) -> Iterator[SourceDocument]:
        """Yield handles for every available document (lazily - no bulk loads)."""

    @abstractmethod
    def read(self, doc: SourceDocument) -> bytes:
        """Return the raw bytes for a document."""


class LocalFolderSource(DocumentSource):
    """PDFs in a local folder (recursive). The POC's stand-in for the portal."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def list_documents(self) -> Iterator[SourceDocument]:
        if not self.root.exists():
            return
        for path in sorted(self.root.rglob("*.pdf")):
            raw = path.read_bytes()
            yield SourceDocument(
                doc_id=path.relative_to(self.root).as_posix(),
                display_name=path.name,
                content_hash=hashlib.sha256(raw).hexdigest(),
            )

    def read(self, doc: SourceDocument) -> bytes:
        return (self.root / doc.doc_id).read_bytes()

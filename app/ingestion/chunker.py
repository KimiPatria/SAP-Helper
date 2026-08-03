"""Structure-aware chunking.

One chunk per note section by default, so a Symptom is never severed from
its context: every chunk is prefixed with the note ID, title, and section
name, and carries full metadata linking it back to the note. Oversized
sections are split on paragraph boundaries with a character overlap rather
than mid-sentence fixed windows.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.ingestion.parser import ParsedNote

# Sections that add retrieval value. "Header Data" is metadata noise as prose;
# its useful part (component) is already extracted into chunk metadata.
_EMBEDDABLE_SECTIONS = (
    "Overview",
    "Symptom",
    "Environment",
    "Reproducing the Issue",
    "Cause",
    "Resolution",
    "Workaround",
    "Keywords",
    "Products",
    "See Also",
)


@dataclass(frozen=True)
class Chunk:
    text: str            # embedded text (includes the contextual header)
    note_id: str
    title: str
    section: str
    component: str
    source_file: str
    chunk_index: int


def _split_long(content: str, max_chars: int, overlap: int) -> list[str]:
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
    parts: list[str] = []
    buf = ""
    for para in paragraphs:
        candidate = f"{buf}\n\n{para}" if buf else para
        if len(candidate) <= max_chars:
            buf = candidate
            continue
        if buf:
            parts.append(buf)
            buf = buf[-overlap:] + "\n\n" + para if overlap else para
        # A single paragraph larger than the window: hard-split as last resort.
        while len(buf) > max_chars:
            parts.append(buf[:max_chars])
            buf = buf[max_chars - overlap:]
    if buf:
        parts.append(buf)
    return parts or [content[:max_chars]]


def chunk_note(note: ParsedNote, max_chars: int, overlap: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    idx = 0
    for section in _EMBEDDABLE_SECTIONS:
        content = note.sections.get(section, "")
        if not content:
            continue
        header = f"SAP Note {note.note_id} - {note.title}\nSection: {section}\n\n"
        for part in _split_long(content, max_chars, overlap):
            chunks.append(
                Chunk(
                    text=header + part,
                    note_id=note.note_id,
                    title=note.title,
                    section=section,
                    component=note.component,
                    source_file=note.source_name,
                    chunk_index=idx,
                )
            )
            idx += 1
    return chunks

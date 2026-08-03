"""Parse SAP Note PDFs into structured fields.

SAP Notes / Knowledge Base Articles follow a recognizable layout:
a header line like "2458901 - Indexserver crashes ..." followed by
sections such as Symptom, Environment, Reproducing the Issue, Cause,
Resolution, Keywords, and header data with the component (e.g. HAN-DB).

Rather than treating a PDF as one blob, we extract those sections so the
chunker can keep a symptom and its resolution associated with the same
note and label every chunk with the section it came from.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from pypdf import PdfReader

# Canonical section names, keyed by the lowercase heading text seen in PDFs.
SECTION_ALIASES: dict[str, str] = {
    "symptom": "Symptom",
    "symptoms": "Symptom",
    "environment": "Environment",
    "reproducing the issue": "Reproducing the Issue",
    "cause": "Cause",
    "root cause": "Cause",
    "resolution": "Resolution",
    "solution": "Resolution",
    "workaround": "Workaround",
    "see also": "See Also",
    "references": "See Also",
    "keywords": "Keywords",
    "other terms": "Keywords",
    "header data": "Header Data",
    "attributes": "Header Data",
    "products": "Products",
    "affected releases": "Products",
}

_NOTE_ID_PATTERNS = [
    re.compile(r"(?:SAP\s+Note|SAP\s+KBA|Note|KBA)\s*#?\s*(\d{6,8})", re.IGNORECASE),
    re.compile(r"^\s*(\d{6,8})\s*[-–]\s+\S", re.MULTILINE),
]
_TITLE_PATTERN = re.compile(r"^\s*\d{6,8}\s*[-–]\s*(.+)$", re.MULTILINE)
# SAP application component codes, e.g. HAN-DB, BC-DB-HDB, FI-AP-AP-Q1
_COMPONENT_PATTERN = re.compile(r"\b([A-Z]{2,3}(?:-[A-Z0-9]{1,8}){1,4})\b")


@dataclass
class ParsedNote:
    note_id: str
    title: str
    component: str
    source_name: str
    sections: dict[str, str] = field(default_factory=dict)  # canonical name -> text


def _extract_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = [(page.extract_text() or "") for page in reader.pages]
    return "\n".join(pages)


def _split_sections(text: str) -> dict[str, str]:
    """Walk the text line by line; short lines matching a known heading start a new section."""
    sections: dict[str, list[str]] = {}
    current = "Overview"
    for line in text.splitlines():
        stripped = line.strip().rstrip(":").strip()
        key = stripped.lower()
        if key in SECTION_ALIASES and len(stripped) <= 40:
            current = SECTION_ALIASES[key]
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return {
        name: body
        for name, lines in sections.items()
        if (body := "\n".join(lines).strip())
    }


def _find_note_id(text: str, source_name: str) -> str:
    head = text[:3000]
    for pattern in _NOTE_ID_PATTERNS:
        m = pattern.search(head)
        if m:
            return m.group(1)
    m = re.search(r"(\d{6,8})", source_name)
    return m.group(1) if m else ""


def _find_title(text: str) -> str:
    m = _TITLE_PATTERN.search(text[:3000])
    if m:
        return m.group(1).strip()
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:150]
    return "Untitled note"


def _find_component(text: str, sections: dict[str, str]) -> str:
    # Prefer an explicit "Component: XX-YY" mention, then the header-data
    # section, then anywhere in the first page.
    m = re.search(r"Component\s*[:\-]?\s*([A-Z]{2,3}(?:-[A-Z0-9]{1,8}){1,4})", text)
    if m:
        return m.group(1)
    for scope in (sections.get("Header Data", ""), text[:2500]):
        m = _COMPONENT_PATTERN.search(scope)
        if m:
            return m.group(1)
    return ""


def parse_note(pdf_bytes: bytes, source_name: str) -> ParsedNote:
    text = _extract_text(pdf_bytes)
    sections = _split_sections(text)
    note_id = _find_note_id(text, source_name)
    return ParsedNote(
        note_id=note_id,
        title=_find_title(text),
        component=_find_component(text, sections),
        source_name=source_name,
        sections=sections,
    )

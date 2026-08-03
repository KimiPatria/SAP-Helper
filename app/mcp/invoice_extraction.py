"""Supplier-invoice field extraction from an image or PDF, with confidences.

Uses the existing swappable GenerationProvider (Bedrock Nova by default) with
vision input - the same abstraction the RAG/chat side uses, so the extractor
is provider-agnostic and swaps with GENERATION_PROVIDER-style config. The
provider must expose `generate_with_vision` (Bedrock does; Groq doesn't yet),
duck-typed exactly like `generate_with_tools`.

Two design choices the brief leaves to judgment, made here and documented in
the README:

* The model returns a value AND a 0-1 confidence per field, as strict JSON.
  We never let the model do arithmetic or "decide" a match - it only reads the
  page. Downstream, any critical field (PO number, PO item, quantity, unit
  price) below the configured threshold routes the whole invoice to a human
  review queue; the pipeline does not proceed on a best guess for those.

* If the PO item number is unreadable, we still capture the line's material
  description so the matcher can FALL BACK to matching on description against
  the PO's lines - but that fallback is marked explicitly in the result and
  never treated as equivalent to a clean item-number match.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Fields we require a confident read of before the pipeline may proceed
# automatically. Everything else (vendor name, currency, description) can be
# soft - a wrong currency is caught by the matcher, a wrong vendor name only
# weakens a sanity check, but a wrong PO/item/qty/price would post bad data.
#
# The split matters: both groups are critical, but only one is a dead end.
# * BLOCKING - nothing downstream can recover a misread PO number, quantity or
#   unit price, so a low confidence on any of them parks the invoice for a human.
# * RECOVERABLE - an unreadable PO *item* number has a designed second route:
#   the material-description fallback against the PO's lines. Sending it straight
#   to review would make that fallback unreachable, so it is only a review
#   trigger when there is no usable description to fall back on either.
BLOCKING_FIELDS = ("po_number", "quantity", "unit_price")
RECOVERABLE_FIELDS = ("po_item",)
CRITICAL_FIELDS = BLOCKING_FIELDS + RECOVERABLE_FIELDS


class ExtractionError(Exception):
    """Raised when the document cannot be read into structured fields."""


@dataclass(frozen=True)
class ExtractedField:
    value: str
    confidence: float


@dataclass(frozen=True)
class ExtractedInvoice:
    po_number: ExtractedField
    po_item: ExtractedField
    quantity: ExtractedField
    unit_price: ExtractedField
    currency: ExtractedField
    vendor_name: ExtractedField
    vendor_invoice_number: ExtractedField
    material_description: ExtractedField
    raw: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            name: {"value": getattr(self, name).value,
                   "confidence": getattr(self, name).confidence}
            for name in (
                "po_number", "po_item", "quantity", "unit_price", "currency",
                "vendor_name", "vendor_invoice_number", "material_description",
            )
        }

    def low_confidence_fields(self, threshold: float) -> list[str]:
        """Every critical field below `threshold`. The honest full report, used
        for the trace and the review record - see `blocking_low_confidence_fields`
        for the subset that actually stops the pipeline."""
        return [
            name for name in CRITICAL_FIELDS
            if getattr(self, name).confidence < threshold
        ]

    def blocking_low_confidence_fields(self, threshold: float) -> list[str]:
        """Low-confidence fields with no recovery route - a non-empty result
        means "send to human review", not "guess"."""
        return [
            name for name in BLOCKING_FIELDS
            if getattr(self, name).confidence < threshold
        ]

    def po_item_is_readable(self, threshold: float) -> bool:
        return bool(self.po_item.value) and self.po_item.confidence >= threshold

    def has_description_fallback(self, threshold: float) -> bool:
        """True when the material description was read well enough to attempt
        the flagged description fallback for an unreadable PO item number.

        It is held to the same confidence threshold on purpose: matching on a
        description we could not read confidently would be exactly the guess the
        brief forbids, and reusing the one configured knob beats inventing a
        second, softer threshold for it.
        """
        return (
            bool(self.material_description.value)
            and self.material_description.confidence >= threshold
        )


# Language is pinned to English in the extraction prompt on purpose: the field
# NAMES and JSON must be stable regardless of the invoice's own language, and
# there is a known unrelated bug where model output drifts across languages.
_EXTRACTION_SYSTEM_PROMPT = (
    "You are a precise accounts-payable data-extraction engine. You are given "
    "one supplier invoice (an image or PDF). Read it and return ONLY a single "
    "JSON object - no prose, no markdown, no code fences. Always respond in "
    "English regardless of the invoice's language.\n\n"
    "For each field return an object {\"value\": <string>, \"confidence\": "
    "<number 0..1>}. Use an empty string for a value you cannot find, with a "
    "low confidence. confidence is YOUR calibrated certainty that the value is "
    "correct: 1.0 only when it is printed unambiguously; below 0.5 when you are "
    "guessing or the text is unclear/handwritten/cut off.\n\n"
    "Fields (use exactly these keys):\n"
    "  po_number: the buyer's Purchase Order number this invoice references "
    "(digits, e.g. 4500000123). Not the vendor's own invoice number.\n"
    "  po_item: the PO line/item number if the invoice states one (e.g. 10). "
    "Empty if not shown.\n"
    "  quantity: the invoiced quantity for that line, as a plain number.\n"
    "  unit_price: the price per unit for that line, as a plain number (no "
    "currency symbol).\n"
    "  currency: the ISO currency code (e.g. USD, EUR).\n"
    "  vendor_name: the supplier/seller company name.\n"
    "  vendor_invoice_number: the vendor's OWN invoice/document number (their "
    "reference, not the PO number).\n"
    "  material_description: the description of the goods/service on the line, "
    "for fallback matching when po_item is missing.\n\n"
    "Return the JSON object and nothing else."
)

_USER_PROMPT = (
    "Extract the fields from this supplier invoice as the specified JSON object. "
    "If the invoice has multiple line items, use the single line that best "
    "matches a purchase-order reference; put its description in "
    "material_description. Numbers must be plain (no thousands separators, no "
    "currency symbols)."
)

_MEDIA_BY_TYPE = {
    "image/jpeg": ("image", "jpeg"),
    "image/jpg": ("image", "jpeg"),
    "image/png": ("image", "png"),
    "image/gif": ("image", "gif"),
    "image/webp": ("image", "webp"),
    "application/pdf": ("document", "pdf"),
}


def _media_spec(content_type: str, data: bytes) -> tuple[str, str]:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _MEDIA_BY_TYPE:
        return _MEDIA_BY_TYPE[ct]
    # Sniff common magic bytes when the caller didn't give a usable type.
    if data[:5] == b"%PDF-":
        return ("document", "pdf")
    if data[:3] == b"\xff\xd8\xff":
        return ("image", "jpeg")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ("image", "png")
    raise ExtractionError(
        f"Unsupported invoice content type '{content_type}'. Send a JPEG/PNG "
        "image or a PDF."
    )


def extract_invoice(
    provider: object,
    data: bytes,
    content_type: str,
    max_tokens: int = 1200,
) -> ExtractedInvoice:
    """Read a supplier invoice into an ExtractedInvoice. Raises ExtractionError
    on an unreadable document or an unparseable model response."""
    if not hasattr(provider, "generate_with_vision"):
        raise ExtractionError(
            "The configured generation provider has no vision support "
            "(need Bedrock/Nova). Extraction cannot run."
        )
    if not data:
        raise ExtractionError("Empty invoice file - nothing to extract.")

    kind, fmt = _media_spec(content_type, data)
    try:
        result = provider.generate_with_vision(
            _EXTRACTION_SYSTEM_PROMPT, _USER_PROMPT, [(kind, fmt, data)], max_tokens,
            # Greedy decoding: this step READS a document, it does not write
            # prose. At the model's sampling default the same invoice extracts
            # differently between runs - a page whose PO item number is read on
            # one run and left blank on the next silently changes which matching
            # route the pipeline takes. Reproducible extracted fields are the
            # whole premise of the confidence gate and the review queue.
            temperature=0.0,
        )
    except Exception as exc:  # GenerationError or provider-specific failure
        raise ExtractionError(f"Vision model could not process the invoice: {exc}") from exc

    parsed = _parse_json_object(result.answer)
    return _to_extracted(parsed)


def _parse_json_object(text: str) -> dict:
    """Pull the JSON object out of the model's answer, tolerating stray fences
    or a leading sentence despite the prompt asking for bare JSON."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ExtractionError(
                f"Model returned text that is not valid JSON: {exc}"
            ) from exc
    raise ExtractionError("Model response contained no JSON object to parse.")


def _to_extracted(parsed: dict) -> ExtractedInvoice:
    def field_of(name: str) -> ExtractedField:
        node = parsed.get(name)
        if isinstance(node, dict):
            value = str(node.get("value", "")).strip()
            conf = _clamp_conf(node.get("confidence"))
        else:
            # Tolerate a model that emitted a bare value without confidence:
            # treat a present value as low-but-nonzero, an absent one as zero.
            value = str(node or "").strip()
            conf = 0.5 if value else 0.0
        return ExtractedField(value=value, confidence=conf)

    return ExtractedInvoice(
        po_number=field_of("po_number"),
        po_item=field_of("po_item"),
        quantity=field_of("quantity"),
        unit_price=field_of("unit_price"),
        currency=field_of("currency"),
        vendor_name=field_of("vendor_name"),
        vendor_invoice_number=field_of("vendor_invoice_number"),
        material_description=field_of("material_description"),
        raw=parsed,
    )


def _clamp_conf(value: object) -> float:
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, conf))

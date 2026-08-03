"""Supplier Invoice posting payload - pure, built against the live $metadata.

Mirrors po.py: no I/O here. This module builds the exact OData body posted to
`API_SUPPLIERINVOICE_PROCESS_SRV`, hashes it for idempotency, renders the
human summary a person approves, and parses SAP's create response.

Ground truth comes from this tenant's `$metadata` (not assumption - the brief
forbids guessing field names on this API):

* Service is OData **v2** (DataServiceVersion 2.0), so:
    - Edm.Decimal values are JSON strings;
    - Edm.DateTime values use the `/Date(<epoch-ms>)/` literal;
    - a deep insert nests child entities under the navigation property as a
      plain array;
    - the create response is wrapped as {"d": {...}}.
* Header entity set: **A_SupplierInvoice** (the only creatable set).
* PO-referenced items ride on the header POST via the navigation property
  **to_SuplrInvcItemPurOrdRef** (entity type A_SuplrInvcItemPurOrdRefType).
* Vendor's own invoice number is **SupplierInvoiceIDByInvcgParty**; the
  supplier is **InvoicingParty**; **TaxIsCalculatedAutomatically** lets SAP
  derive tax from each line's **TaxCode**.

Tax/gross caveat (documented in the README): the POC posts one tax code per
line and sets InvoiceGrossAmount = net total, which balances only when that
tax code is 0%-rated (e.g. V0 on the demo tenant). A non-zero rate needs the
gross grossed-up, or SAP's balance check rejects the document.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

# Field/entity names verified against the tenant's $metadata (see module docstring).
HEADER_ENTITY_SET = "A_SupplierInvoice"
ITEM_NAV_PROPERTY = "to_SuplrInvcItemPurOrdRef"


@dataclass(frozen=True)
class ProposedInvoiceItem:
    supplier_invoice_item: str   # "1", "2", ... (sequential)
    purchase_order: str
    purchase_order_item: str
    quantity: str                # invoiced qty, decimal string
    unit: str                    # PurchaseOrderQuantityUnit (e.g. PC, EA)
    amount: str                  # net line amount (qty * unit price), decimal string
    tax_code: str
    gr_document: str = ""        # ReferenceDocument - the Material Document (GR) backing this line
    gr_fiscal_year: str = ""     # ReferenceDocumentFiscalYear - that Material Document's fiscal year
    gr_item: str = ""            # ReferenceDocumentItem - that Material Document's item
    # Which reference shape SAP requires for THIS line, decided by the PO item's
    # GR-based-IV flag in invoice_preflight (REFERENCE_MODE_PO / REFERENCE_MODE_GR).
    # There is no default: an unset value raises in the payload builder rather
    # than falling back to a shape that may be wrong - see the builder's note.
    reference_mode: str = ""


@dataclass(frozen=True)
class ProposedInvoice:
    company_code: str
    document_date: str           # ISO 'YYYY-MM-DD' (invoice date on the document)
    posting_date: str            # ISO 'YYYY-MM-DD'
    tax_determination_date: str  # ISO 'YYYY-MM-DD' - TaxDeterminationDate (header)
    invoicing_party: str         # supplier code (from the PO)
    vendor_invoice_number: str   # SupplierInvoiceIDByInvcgParty
    currency: str
    gross_amount: str            # InvoiceGrossAmount (net total when tax is 0%)
    items: tuple[ProposedInvoiceItem, ...] = field(default_factory=tuple)


def _odata_date(value: str) -> str:
    """ISO date -> OData v2 '/Date(epoch-ms)/' at UTC midnight."""
    try:
        d = date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        d = datetime.now(timezone.utc).date()
    epoch_ms = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
    return f"/Date({epoch_ms})/"


def build_supplier_invoice_payload(proposed: ProposedInvoice) -> dict:
    """The exact deep-insert body POSTed to A_SupplierInvoice.

    Each item's reference shape is chosen by its `reference_mode`, which comes
    from the PO item's GR-based Invoice Verification flag (invoice_preflight):

    * REFERENCE_MODE_PO (GR-based IV OFF) - reference the PO line only. The
      ReferenceDocument/-FiscalYear/-Item keys are OMITTED ENTIRELY, not sent
      blank: SAP's check is on their presence, and filling them on a PO-based
      item is rejected with "Only fill ReferenceDocument/-FiscalYear/-Item if
      GR-based IV is active" together with "Item not selectable".
    * REFERENCE_MODE_GR (GR-based IV ON) - additionally reference the goods
      receipt. SAP's MRM_FRSEG_CHECK validates that FRSEG-mirrored triple
      independently of PurchaseOrder/PurchaseOrderItem, and it must be the
      actual Material Document backing the line, not the PO number.

    An unset reference_mode raises. Defaulting it either way would reintroduce
    exactly the bug this branch fixes - and silently, since the payload would
    still look well-formed right up until SAP rejected it.
    """
    from app.mcp.invoice_preflight import REFERENCE_MODE_GR, REFERENCE_MODE_PO

    items = []
    for item in proposed.items:
        if item.reference_mode not in (REFERENCE_MODE_PO, REFERENCE_MODE_GR):
            raise ValueError(
                f"Invoice item {item.supplier_invoice_item} has no resolved "
                f"reference_mode (got {item.reference_mode!r}). It must be set from "
                "the PO item's GR-based-IV flag via invoice_preflight.run_preflight "
                "before a payload can be built."
            )
        body = {
            "SupplierInvoiceItem": item.supplier_invoice_item,
            "PurchaseOrder": item.purchase_order,
            "PurchaseOrderItem": item.purchase_order_item,
            "DocumentCurrency": proposed.currency,
            "SupplierInvoiceItemAmount": item.amount,
            "QuantityInPurchaseOrderUnit": item.quantity,
        }
        if item.reference_mode == REFERENCE_MODE_GR:
            body["ReferenceDocument"] = item.gr_document
            body["ReferenceDocumentFiscalYear"] = item.gr_fiscal_year
            body["ReferenceDocumentItem"] = item.gr_item
        if item.unit:
            body["PurchaseOrderQuantityUnit"] = item.unit
        if item.tax_code:
            body["TaxCode"] = item.tax_code
        items.append(body)
    return {
        "CompanyCode": proposed.company_code,
        "DocumentDate": _odata_date(proposed.document_date),
        "PostingDate": _odata_date(proposed.posting_date),
        "TaxDeterminationDate": _odata_date(proposed.tax_determination_date),
        "InvoicingParty": proposed.invoicing_party,
        "SupplierInvoiceIDByInvcgParty": proposed.vendor_invoice_number,
        "DocumentCurrency": proposed.currency,
        "InvoiceGrossAmount": proposed.gross_amount,
        "TaxIsCalculatedAutomatically": True,
        ITEM_NAV_PROPERTY: items,
    }


# ---- editable-field rows for the approval UI -------------------------------
#
# SAP field names/labels here are taken verbatim from this tenant's live
# $metadata (sap:label on each Property), not guessed - same ground-truth rule
# as the payload builder above. PO/PO-item are excluded from edit support on
# purpose: they are what the 3-way match resolved, and hand-editing either
# would silently detach the posting from the match that approved it.

_ITEM_EDIT_SUFFIXES = {"quantity": "quantity", "unit": "unit", "amount": "amount", "tax_code": "tax_code"}
_LOCKED_ITEM_SUFFIXES = ("po", "po_item")

_HEADER_EDIT_FIELDS = {
    "vendor_invoice_number": "vendor_invoice_number",
    "company_code": "company_code",
    "document_date": "document_date",
    "posting_date": "posting_date",
    "tax_determination_date": "tax_determination_date",
    "currency": "currency",
    "gross_amount": "gross_amount",
}


def _conf_pct(confidences: dict, *keys: str) -> float | None:
    """Lowest OCR confidence among the fields a value was derived from (e.g. a
    line amount depends on both quantity and unit price - show the weaker of
    the two, not an average that hides the worse read)."""
    vals = [confidences[k] for k in keys if confidences.get(k) is not None]
    if not vals:
        return None
    return round(min(vals) * 100, 1)


def build_proposal_fields(proposed: ProposedInvoice, confidences: dict | None = None) -> list[dict]:
    """SAP-labelled rows for the approval UI: technical field name, human
    label, current value, whether a human may edit it, and where the value
    came from (OCR read / matched PO / system default) so a confidence score
    is only ever shown where one is real."""
    confidences = confidences or {}

    def row(section, key, sap_field, label, value, editable, kind, conf_keys=()):
        return {
            "section": section,
            "key": key,
            "sap_field": sap_field,
            "label": label,
            "value": value,
            "editable": editable,
            "confidence_kind": kind,  # "ocr" | "po" | "system" | "locked"
            "confidence_pct": _conf_pct(confidences, *conf_keys) if kind == "ocr" else None,
        }

    rows = [
        row("header", "invoicing_party", "InvoicingParty", "Supplier",
            proposed.invoicing_party, True, "po"),
        row("header", "vendor_invoice_number", "SupplierInvoiceIDByInvcgParty", "Vendor Invoice No.",
            proposed.vendor_invoice_number, True, "ocr", ("vendor_invoice_number",)),
        row("header", "company_code", "CompanyCode", "Company Code",
            proposed.company_code, True, "po"),
        row("header", "document_date", "DocumentDate", "Invoice Date",
            proposed.document_date, True, "system"),
        row("header", "posting_date", "PostingDate", "Posting Date",
            proposed.posting_date, True, "system"),
        row("header", "tax_determination_date", "TaxDeterminationDate", "Tax Determination Date",
            proposed.tax_determination_date, True, "system"),
        row("header", "currency", "DocumentCurrency", "Currency",
            proposed.currency, True, "ocr", ("currency",)),
        row("header", "gross_amount", "InvoiceGrossAmount", "Gross Amount",
            proposed.gross_amount, True, "ocr", ("quantity", "unit_price")),
    ]
    for item in proposed.items:
        idx = item.supplier_invoice_item
        rows += [
            row("item", f"item_{idx}_po", "PurchaseOrder", "PO Number",
                item.purchase_order, False, "locked"),
            row("item", f"item_{idx}_po_item", "PurchaseOrderItem", "PO Item",
                item.purchase_order_item, False, "locked"),
            row("item", f"item_{idx}_quantity", "QuantityInPurchaseOrderUnit", "Quantity",
                item.quantity, True, "ocr", ("quantity",)),
            row("item", f"item_{idx}_unit", "PurchaseOrderQuantityUnit", "Unit",
                item.unit, True, "po"),
            row("item", f"item_{idx}_amount", "SupplierInvoiceItemAmount", "Item Amount",
                item.amount, True, "ocr", ("quantity", "unit_price")),
            row("item", f"item_{idx}_tax_code", "TaxCode", "Tax Code",
                item.tax_code, True, "system"),
            # Locked and SAP-determined, but shown: it is the difference between
            # a posting SAP accepts and one it rejects, and an approver should be
            # able to see which shape was chosen and why.
            row("item", f"item_{idx}_reference_mode", "InvoiceIsGoodsReceiptBased",
                "Reference Basis",
                "Goods receipt (GR-based IV active)"
                if item.reference_mode == "gr-based"
                else "Purchase order line (GR-based IV not active)",
                False, "locked"),
        ]
        if item.reference_mode == "gr-based":
            rows.append(
                row("item", f"item_{idx}_gr_reference", "ReferenceDocument",
                    "Goods Receipt Reference",
                    f"{item.gr_document}/{item.gr_fiscal_year} item {item.gr_item}",
                    False, "locked")
            )
    return rows


def apply_field_edits(proposed: ProposedInvoice, edits: dict) -> tuple[ProposedInvoice, list[str]]:
    """Apply {row_key: new_value} onto a ProposedInvoice, returning the updated
    (still-immutable) copy and any keys rejected because they are unknown or
    locked (PurchaseOrder/PurchaseOrderItem - see module note above)."""
    from dataclasses import replace

    header_changes: dict = {}
    items = list(proposed.items)
    rejected: list[str] = []
    for key, value in edits.items():
        if key in _HEADER_EDIT_FIELDS:
            header_changes[_HEADER_EDIT_FIELDS[key]] = str(value)
            continue
        matched = False
        for i, item in enumerate(items):
            prefix = f"item_{item.supplier_invoice_item}_"
            if not key.startswith(prefix):
                continue
            matched = True
            suffix = key[len(prefix):]
            if suffix in _LOCKED_ITEM_SUFFIXES:
                rejected.append(key)
            elif suffix in _ITEM_EDIT_SUFFIXES:
                items[i] = replace(item, **{_ITEM_EDIT_SUFFIXES[suffix]: str(value)})
            else:
                rejected.append(key)
            break
        if not matched:
            rejected.append(key)

    return replace(proposed, items=tuple(items), **header_changes), rejected


def payload_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sum_amounts(items: tuple[ProposedInvoiceItem, ...]) -> str:
    total = Decimal("0")
    for item in items:
        try:
            total += Decimal(item.amount)
        except InvalidOperation:
            continue
    return format(total.normalize(), "f")


def render_invoice_preview(
    proposed: ProposedInvoice,
    match_summary: str,
    warnings: list[str],
) -> str:
    """The human-readable proposal a person approves before commit."""
    lines = [
        "SUPPLIER INVOICE PROPOSAL - nothing has been posted to SAP yet",
        f"Supplier (InvoicingParty): {proposed.invoicing_party}",
        f"Vendor invoice no.:        {proposed.vendor_invoice_number}",
        f"Company code:              {proposed.company_code}",
        f"Invoice date / posting:    {proposed.document_date} / {proposed.posting_date}",
        f"Tax determination date:    {proposed.tax_determination_date}",
        f"Currency:                  {proposed.currency}",
        f"Gross amount:              {proposed.gross_amount} {proposed.currency}",
        "Lines:",
    ]
    for item in proposed.items:
        lines.append(
            f"  {item.supplier_invoice_item:>3}  PO {item.purchase_order}/"
            f"{item.purchase_order_item}  |  qty {item.quantity} {item.unit}  |  "
            f"amount {item.amount} {proposed.currency}  |  tax {item.tax_code or '(none)'}"
        )
        # Which shape SAP is being sent, spelled out rather than left implicit -
        # posting the wrong one is a hard rejection (see the payload builder).
        lines.append(
            f"       referenced against goods receipt {item.gr_document}/"
            f"{item.gr_fiscal_year} item {item.gr_item} (GR-based invoice verification)"
            if item.reference_mode == "gr-based" else
            "       referenced against the PO line only "
            "(GR-based invoice verification is not active on this item)"
        )
    lines.append(f"Match status: {match_summary}")
    if warnings:
        lines.append("Please note:")
        lines.extend(f"  - {w}" for w in warnings)
    return "\n".join(lines)


def parse_supplier_invoice_response(data: dict) -> dict:
    """Pull the assigned document key from the v2 create response."""
    record = data.get("d", data) or {}
    return {
        "supplier_invoice": str(record.get("SupplierInvoice", "")).strip(),
        "fiscal_year": str(record.get("FiscalYear", "")).strip(),
        "company_code": record.get("CompanyCode", ""),
        "invoicing_party": record.get("InvoicingParty", ""),
        "currency": record.get("DocumentCurrency", ""),
        "gross_amount": str(record.get("InvoiceGrossAmount", "")),
        "status_code": str(record.get("SupplierInvoiceStatus", "")).strip(),
    }

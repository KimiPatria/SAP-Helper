"""Purchase Order domain logic - pure functions, no I/O.

Everything here operates on already-resolved codes (the resolver's output);
building the OData payload, hashing it, rendering the human preview, and
parsing SAP's create/read responses. Keeping this free of HTTP makes the
preview -> create contract testable without a tenant.

Payload notes (API_PURCHASE_ORDER_2, OData v4 deep insert):
* Edm.Decimal values (quantities, prices) must be JSON *strings*.
* Items ride along on the header POST via the `_PurchaseOrderItem`
  navigation property (v4 naming - the older v2 API_PURCHASEORDER_PROCESS_SRV
  used `to_PurchaseOrderItem` instead; swap it back if that's your tenant's
  released service).
* Omitted optional fields (unit, price) are filled by SAP from the
  material master / purchasing info record - deliberately, so the POC
  doesn't fight tenant pricing configuration.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class ResolvedItem:
    material_code: str
    material_description: str
    quantity: str            # normalized decimal string, e.g. "10" or "2.5"
    unit: str = ""           # ISO/SAP unit like EA, PC, KG; "" -> SAP derives
    net_price: str = ""      # "" -> SAP prices from the info record


@dataclass(frozen=True)
class ResolvedPO:
    supplier_code: str
    supplier_name: str
    plant_code: str
    plant_name: str
    company_code: str
    purchasing_org: str
    purchasing_group: str
    order_type: str
    currency: str
    items: tuple[ResolvedItem, ...] = field(default_factory=tuple)


def normalize_quantity(value: object, what: str) -> str:
    """Accepts int/float/str; returns a plain decimal string SAP accepts.

    Raises ValueError with an agent-actionable message on junk input.
    """
    try:
        quantity = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValueError(
            f"{what} must be a number (got {value!r}). "
            "Send e.g. 10 or \"2.5\" - no units inside the number."
        ) from None
    if quantity <= 0:
        raise ValueError(f"{what} must be greater than zero (got {value!r}).")
    return format(quantity.normalize(), "f")


def build_odata_payload(po: ResolvedPO) -> dict:
    """The exact body POSTed to PurchaseOrder (deep insert with items)."""
    items = []
    for index, item in enumerate(po.items):
        body = {
            "PurchaseOrderItem": str((index + 1) * 10),
            "Material": item.material_code,
            "Plant": po.plant_code,
            "OrderQuantity": item.quantity,
            "DocumentCurrency": po.currency,
        }
        if item.unit:
            body["PurchaseOrderQuantityUnit"] = item.unit
        if item.net_price:
            body["NetPriceAmount"] = item.net_price
        items.append(body)
    return {
        "CompanyCode": po.company_code,
        "PurchaseOrderType": po.order_type,
        "Supplier": po.supplier_code,
        "PurchasingOrganization": po.purchasing_org,
        "PurchasingGroup": po.purchasing_group,
        "DocumentCurrency": po.currency,
        "_PurchaseOrderItem": items,
    }


def payload_hash(payload: dict) -> str:
    """Canonical hash of the resolved payload - the idempotency identity."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render_preview(po: ResolvedPO, warnings: list[str]) -> str:
    """The human-readable summary the user must confirm before create."""
    lines = [
        "PURCHASE ORDER PREVIEW - nothing has been created in SAP yet",
        f"Supplier:       {po.supplier_code} - {po.supplier_name or '(name unavailable)'}",
        f"Plant:          {po.plant_code} - {po.plant_name or '(name unavailable)'}",
        f"Order type:     {po.order_type}    Company code: {po.company_code}",
        f"Purchasing:     org {po.purchasing_org} / group {po.purchasing_group}    "
        f"Currency: {po.currency}",
        "Items:",
    ]
    for index, item in enumerate(po.items):
        price = f"{item.net_price} {po.currency}/unit" if item.net_price \
            else "from SAP info record"
        unit = item.unit or "(unit from material master)"
        lines.append(
            f"  {(index + 1) * 10:>4}  {item.material_code} - "
            f"{item.material_description or '(description unavailable)'}  |  "
            f"qty {item.quantity} {unit}  |  price: {price}"
        )
    if warnings:
        lines.append("Please note:")
        lines.extend(f"  - {w}" for w in warnings)
    return "\n".join(lines)


# ---- response parsing ----------------------------------------------------

# Common PurchasingProcessingStatus values. Tenant-release dependent -
# the raw code is always included so an unmapped value is still visible.
_PROCESSING_STATUS = {
    "01": "reserved",
    "02": "held (parked, not yet ordered)",
    "03": "in approval",
    "05": "ordered (released to supplier)",
    "08": "follow-on document exists",
}

_V2_DATE = re.compile(r"/Date\((\-?\d+)\)/")


# Values SAP uses for "true" across its dialects: OData v4 returns a real JSON
# boolean, v2 returns the string "true", and some flat exports still carry the
# classic ABAP 'X'. Anything unrecognised is deliberately NOT coerced.
_TRUE_TOKENS = frozenset({"true", "x", "1", "yes"})
_FALSE_TOKENS = frozenset({"false", "0", "no"})


def _flag(record: dict, key: str) -> bool | None:
    """Tri-state read of a SAP boolean: True / False / None when the tenant did
    not send the field at all (older service release, or a $select that omitted it).

    None is a first-class answer here, not a convenience default. The invoice
    pre-flight (invoice_preflight.py) treats an unknown GR-based-IV flag as a
    BLOCKER rather than assuming either reference shape - guessing it wrong is
    exactly the bug this tri-state exists to prevent.
    """
    if key not in record:
        return None
    value = record[key]
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    token = str(value).strip().lower()
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    # Includes "" - an empty string is an absent value here, not a False one.
    return None


def _plain_date(value: object) -> str:
    """OData v2 serializes dates as /Date(epoch-millis)/; v4 already sends a
    plain ISO-8601 string, so the fallback below is the normal v4 path."""
    match = _V2_DATE.fullmatch(str(value or ""))
    if match:
        from datetime import datetime, timezone

        epoch_seconds = int(match.group(1)) / 1000
        return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).date().isoformat()
    return str(value or "")


def parse_create_response(data: dict) -> dict:
    record = data.get("d", data) or {}
    return {
        "purchase_order": str(record.get("PurchaseOrder", "")).strip(),
        "order_type": record.get("PurchaseOrderType", ""),
        "supplier": record.get("Supplier", ""),
        "currency": record.get("DocumentCurrency", ""),
    }


def parse_po_status(data: dict) -> dict:
    record = data.get("d", data) or {}
    raw_status = str(record.get("PurchasingProcessingStatus", "")).strip()
    items_node = record.get("_PurchaseOrderItem") or {}
    if isinstance(items_node, dict):
        # v2 wraps expanded collections as {"results": [...]}; v4 returns a
        # plain list directly, so this branch is a no-op on v4 responses.
        items_node = items_node.get("results") or []
    items = [
        {
            "item": i.get("PurchaseOrderItem", ""),
            "material": i.get("Material", ""),
            # The line's free TEXT, distinct from the material CODE above. The
            # invoice matcher's description fallback (invoice_matching.find_po_line)
            # searches this; without it that fallback could only ever compare an
            # invoice's prose against a code like "EGM002" and so never matched.
            # PurchaseOrderItemText is the v4 field; the alternatives cover the v2
            # service and tenants that expand the product description instead.
            "description": str(
                i.get("PurchaseOrderItemText")
                or i.get("ProductName")
                or i.get("MaterialDescription")
                or ""
            ),
            "plant": i.get("Plant", ""),
            "quantity": i.get("OrderQuantity", ""),
            "unit": i.get("PurchaseOrderQuantityUnit", ""),
            "net_price": i.get("NetPriceAmount", ""),
            "deleted": bool(i.get("PurchasingDocumentDeletionCode")),
            # ---- supplier-invoice posting prerequisites -----------------
            # Names confirmed against this tenant's live $metadata for
            # API_PURCHASE_ORDER_2 (EntityType PurchaseOrderItem_Type), not
            # assumed. All are Edm.Boolean and arrive as real JSON booleans on
            # v4; _flag keeps "field absent" distinguishable from False.
            #
            # gr_based_iv is the one that decides an invoice item's REFERENCE
            # SHAPE (invoice_posting.build_supplier_invoice_payload):
            #   False -> PO-based: reference PurchaseOrder + PurchaseOrderItem only.
            #   True  -> GR-based: additionally reference the goods-receipt
            #            material document (ReferenceDocument/-FiscalYear/-Item).
            # Sending the GR triple on a PO-based item is what SAP rejects with
            # "Only fill ReferenceDocument/-FiscalYear/-Item if GR-based IV is
            # active" plus "Item not selectable".
            "gr_based_iv": _flag(i, "InvoiceIsGoodsReceiptBased"),
            "invoice_expected": _flag(i, "InvoiceIsExpected"),
            "finally_invoiced": _flag(i, "IsFinallyInvoiced"),
            "gr_expected": _flag(i, "GoodsReceiptIsExpected"),
            "service_based_iv": _flag(i, "InvoiceIsMMServiceEntryBased"),
        }
        for i in items_node
    ]
    return {
        "purchase_order": str(record.get("PurchaseOrder", "")).strip(),
        "order_type": record.get("PurchaseOrderType", ""),
        "supplier": record.get("Supplier", ""),
        "supplier_name": record.get("SupplierName", ""),
        "company_code": record.get("CompanyCode", ""),
        "currency": record.get("DocumentCurrency", ""),
        "net_amount": str(record.get("PurchaseOrderNetAmount", "")),
        "created_on": _plain_date(record.get("CreationDate")),
        "status_code": raw_status,
        "status": _PROCESSING_STATUS.get(raw_status,
                                         "unknown - see status_code" if raw_status else ""),
        # Header-level posting prerequisites, same provenance as the item ones
        # above (EntityType PurchaseOrder_Type). PaymentTerms is Edm.String and
        # is REQUIRED for invoice posting; it is inherited from the vendor
        # master at PO creation, so an empty value here is an upstream
        # data-quality gap the pre-flight surfaces rather than guesses at.
        "payment_terms": str(record.get("PaymentTerms", "") or "").strip(),
        "deletion_code": str(record.get("PurchaseOrderDeletionCode", "") or "").strip(),
        "release_incomplete": _flag(record, "ReleaseIsNotCompleted"),
        "items": items,
    }


# ---- PO <-> Goods Receipt two-way match -----------------------------------
# The third leg (Supplier Invoice) isn't authorized on this tenant yet - this
# compares only what's available: ordered (PO) vs net received (GR).


def compare_po_to_receipts(po_status: dict, gr_rows: list[dict]) -> dict:
    """Per-item ordered vs. net-received quantity. Pure function - `po_status`
    is parse_po_status()'s output; `gr_rows` is raw A_MaterialDocumentItem
    rows (fetch_goods_receipt_items) for the same PO.

    Movements net via DebitCreditCode (S = receipt, H = reversal) rather than
    being dropped, so a receipt that was later reversed nets back to zero
    instead of counting as received.
    """
    received_by_item: dict[str, Decimal] = {}
    for row in gr_rows:
        item = str(row.get("PurchaseOrderItem", "")).strip()
        if not item:
            continue
        try:
            qty = Decimal(str(row.get("QuantityInEntryUnit", "0") or "0"))
        except InvalidOperation:
            continue
        sign = -1 if row.get("DebitCreditCode") == "H" else 1
        received_by_item[item] = received_by_item.get(item, Decimal("0")) + sign * qty

    items = []
    fully_matched = True
    for po_item in po_status.get("items", []):
        item_no = str(po_item.get("item", "")).strip()
        try:
            ordered = Decimal(str(po_item.get("quantity") or "0"))
        except InvalidOperation:
            ordered = Decimal("0")
        received = received_by_item.get(item_no, Decimal("0"))
        unit = po_item.get("unit", "")
        if received <= 0:
            status, matched = "not yet received", False
        elif received < ordered:
            status, matched = f"short by {ordered - received} {unit}".strip(), False
        elif received > ordered:
            status, matched = f"over-received by {received - ordered} {unit}".strip(), False
        else:
            status, matched = "match", True
        fully_matched = fully_matched and matched
        items.append({
            "item": item_no,
            "material": po_item.get("material", ""),
            "ordered": str(ordered),
            "received": str(received),
            "unit": unit,
            "status": status,
            "matched": matched,
        })
    return {
        "purchase_order": po_status.get("purchase_order", ""),
        "supplier": po_status.get("supplier", ""),
        "supplier_name": po_status.get("supplier_name", ""),
        "items": items,
        "fully_matched": fully_matched and bool(items),
    }

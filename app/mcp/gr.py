"""Goods Receipt (Material Document) read access - API_MATERIAL_DOCUMENT_SRV.

Read-only: this module only fetches Material Document items for a given PO,
it never posts anything. Each item already carries the originating
PurchaseOrder/PurchaseOrderItem (OData v2 service, {"d": {"results": [...]}}
envelope like the supplier/product lookups).
"""

from __future__ import annotations

from app.mcp.client import SAPClient

_ITEM_FIELDS = (
    "MaterialDocument,MaterialDocumentYear,MaterialDocumentItem,PurchaseOrder,"
    "PurchaseOrderItem,Material,Plant,QuantityInEntryUnit,EntryUnit,"
    "GoodsMovementType,DebitCreditCode,GoodsMovementIsCancelled"
)


def fetch_goods_receipt_items(
    client: SAPClient, service_root: str, purchase_order: str
) -> list[dict]:
    """All Material Document items referencing this PO, cancelled or not -
    the caller nets cancellations out (GoodsMovementIsCancelled / DebitCreditCode)."""
    escaped = purchase_order.replace("'", "''")
    data = client.get(
        f"{service_root}/A_MaterialDocumentItem",
        params={
            "$filter": f"PurchaseOrder eq '{escaped}'",
            "$select": _ITEM_FIELDS,
        },
        context=f"reading goods receipts for purchase order {purchase_order}",
    )
    return data.get("d", {}).get("results") or []

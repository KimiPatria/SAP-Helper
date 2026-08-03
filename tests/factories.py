"""Builders for the data shapes the pure functions consume.

Every factory returns the EXACT shape the production parser emits, so a test
that passes here is evidence about production behaviour rather than about a
shape invented for the test. Where a factory mirrors a parser's output, the
matching seam test in test_seams.py pins the two together - if parse_po_status
stops emitting a key, that test fails even though these factories still supply
it.

Defaults describe a boring, healthy document (one line, ordered 10, received
10, priced 25.00). Each test overrides only the field it is about, which keeps
the interesting value visible at the call site.
"""

from __future__ import annotations


# ---- Purchase Order (parse_po_status output shape) ------------------------


def po_item(**overrides) -> dict:
    """One line of parse_po_status()'s `items` list."""
    item = {
        "item": "10",
        "material": "EGM002",
        "description": "Hex bolts M8 stainless",
        "plant": "1710",
        "quantity": "10",
        "unit": "PC",
        "net_price": "25.00",
        "deleted": False,
        # Posting prerequisites. The default is PO-based invoice verification
        # (gr_based_iv False), which is what both real POs on this tenant
        # actually carry - so the default factory reflects the tenant rather
        # than the shape the old code assumed.
        "gr_based_iv": False,
        "invoice_expected": True,
        "finally_invoiced": False,
        "gr_expected": True,
        "service_based_iv": False,
    }
    item.update(overrides)
    return item


def po_status(items: list[dict] | None = None, **overrides) -> dict:
    """parse_po_status() output. `items` defaults to a single healthy line."""
    status = {
        "purchase_order": "4500000002",
        "order_type": "NB",
        "supplier": "TST02",
        "supplier_name": "",          # blank is REAL on this tenant (BP API 403)
        "company_code": "M100",
        "currency": "IDR",
        "net_amount": "250.00",
        "created_on": "2026-07-01",
        "status_code": "05",
        "status": "ordered (released to supplier)",
        # Header posting prerequisites. Payment terms are POPULATED here on
        # purpose - the factory describes a healthy, postable document, so a
        # test about missing terms has to say so explicitly and is visible at
        # the call site. (Both live POs on this tenant have them blank, which
        # is the data-quality gap bug 2 surfaces.)
        "payment_terms": "0001",
        "deletion_code": "",
        "release_incomplete": False,
        "items": [po_item()] if items is None else items,
    }
    status.update(overrides)
    return status


# ---- Goods Receipt (raw A_MaterialDocumentItem rows) ----------------------


def gr_row(**overrides) -> dict:
    """One raw goods-receipt row as fetch_goods_receipt_items returns it.

    DebitCreditCode "S" = receipt, "H" = reversal - compare_po_to_receipts nets
    them rather than dropping reversals, which is what makes a
    received-then-reversed line read as 0 instead of as delivered.
    """
    row = {
        "PurchaseOrder": "4500000002",
        "PurchaseOrderItem": "10",
        "QuantityInEntryUnit": "10",
        "DebitCreditCode": "S",
        "MaterialDocument": "5000000123",
        "MaterialDocumentYear": "2026",
        "MaterialDocumentItem": "1",
        "GoodsMovementIsCancelled": False,
    }
    row.update(overrides)
    return row


# ---- analysis.py entry shape ---------------------------------------------


def analysis_entry(status: dict | None = None, gr_rows: list[dict] | None = None) -> dict:
    """The {po_status, match, gr_rows} triple analysis.py consumes, with `match`
    computed by the real compare_po_to_receipts rather than hand-written - so
    these tests exercise the same composition server.py::_gather_po_matches does.
    """
    from app.mcp.po import compare_po_to_receipts

    status = po_status() if status is None else status
    rows = [gr_row()] if gr_rows is None else gr_rows
    return {
        "po_status": status,
        "match": compare_po_to_receipts(status, rows),
        "gr_rows": rows,
    }


# ---- extracted invoice (match_invoice's `invoice` argument) ---------------


def invoice(**overrides) -> dict:
    """The VALUES-only dict match_invoice takes (no confidences - those are
    stripped upstream in invoice_tools). Defaults agree with po_item() above,
    so the baseline is a clean 3-way match and any variance in a test is the
    thing that test deliberately changed."""
    inv = {
        "po_number": "4500000002",
        "po_item": "10",
        "quantity": "10",
        "unit_price": "25.00",
        "currency": "IDR",
        "vendor_name": "Test Supplier",
        "vendor_invoice_number": "INV-0042",
        "material_description": "Hex bolts M8 stainless",
    }
    inv.update(overrides)
    return inv


# ---- raw OData payloads (parser input, for end-to-end seam tests) ---------


def odata_po_v4(items: list[dict] | None = None, **overrides) -> dict:
    """A raw API_PURCHASE_ORDER_2 (OData v4) response body, i.e. what
    parse_po_status actually receives. Used by the seam tests that must run
    through the real parser instead of starting from its assumed output."""
    body = {
        "PurchaseOrder": "4500000002",
        "PurchaseOrderType": "NB",
        "Supplier": "TST02",
        "CompanyCode": "M100",
        "DocumentCurrency": "IDR",
        "PurchaseOrderNetAmount": "250.00",
        "CreationDate": "2026-07-01",
        "PurchasingProcessingStatus": "05",
        # SAP field names below are the ones confirmed against this tenant's live
        # $metadata for API_PURCHASE_ORDER_2 - PaymentTerms on PurchaseOrder_Type,
        # InvoiceIsGoodsReceiptBased et al. on PurchaseOrderItem_Type. v4 sends
        # the booleans as real JSON booleans, as written here.
        "PaymentTerms": "0001",
        "PurchaseOrderDeletionCode": "",
        "ReleaseIsNotCompleted": False,
        "_PurchaseOrderItem": [
            {
                "PurchaseOrderItem": "10",
                "Material": "EGM002",
                "PurchaseOrderItemText": "Hex bolts M8 stainless",
                "Plant": "1710",
                "OrderQuantity": "10",
                "PurchaseOrderQuantityUnit": "PC",
                "NetPriceAmount": "25.00",
                "InvoiceIsGoodsReceiptBased": False,
                "InvoiceIsExpected": True,
                "IsFinallyInvoiced": False,
                "GoodsReceiptIsExpected": True,
                "InvoiceIsMMServiceEntryBased": False,
            }
        ] if items is None else items,
    }
    body.update(overrides)
    return body

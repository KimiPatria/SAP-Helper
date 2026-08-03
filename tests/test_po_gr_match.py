"""compare_po_to_receipts - the PO<->GR 2-way match every other feature reuses.

This function is load-bearing three times over: the chat's
compare_po_to_goods_receipts tool, the received-quantity leg of the invoice
3-way match, and all three analysis reports. A silent change here moves
numbers in all of them at once, which is why it gets the most direct tests.
"""

from __future__ import annotations

from factories import gr_row, po_item, po_status

from app.mcp.po import compare_po_to_receipts, parse_po_status


def test_exact_receipt_matches():
    result = compare_po_to_receipts(po_status(), [gr_row()])
    assert result["fully_matched"] is True
    assert result["items"][0]["status"] == "match"
    assert result["items"][0]["received"] == "10"


def test_short_delivery_is_not_matched():
    result = compare_po_to_receipts(po_status(), [gr_row(QuantityInEntryUnit="4")])
    item = result["items"][0]
    assert item["matched"] is False
    assert item["status"] == "short by 6 PC"
    assert result["fully_matched"] is False


def test_over_delivery_is_not_matched():
    result = compare_po_to_receipts(po_status(), [gr_row(QuantityInEntryUnit="12")])
    assert result["items"][0]["status"] == "over-received by 2 PC"
    assert result["items"][0]["matched"] is False


def test_reversal_nets_against_the_receipt():
    """A receipt later reversed must read as NOT received. Dropping 'H' rows
    instead of subtracting them would report the goods as delivered - the
    single most consequential arithmetic detail in this function."""
    rows = [
        gr_row(QuantityInEntryUnit="10", DebitCreditCode="S"),
        gr_row(QuantityInEntryUnit="10", DebitCreditCode="H", MaterialDocument="5000000124"),
    ]
    result = compare_po_to_receipts(po_status(), rows)
    assert result["items"][0]["received"] == "0"
    assert result["items"][0]["status"] == "not yet received"


def test_partial_receipts_accumulate():
    rows = [
        gr_row(QuantityInEntryUnit="4"),
        gr_row(QuantityInEntryUnit="6", MaterialDocument="5000000124"),
    ]
    result = compare_po_to_receipts(po_status(), rows)
    assert result["items"][0]["received"] == "10"
    assert result["fully_matched"] is True


def test_receipts_are_attributed_per_line_not_pooled():
    """Two lines, one receipt each: the quantities must not cross-contaminate."""
    status = po_status(items=[
        po_item(item="10", quantity="10"),
        po_item(item="20", quantity="5", material="EGM003"),
    ])
    rows = [
        gr_row(PurchaseOrderItem="10", QuantityInEntryUnit="10"),
        gr_row(PurchaseOrderItem="20", QuantityInEntryUnit="5"),
    ]
    result = compare_po_to_receipts(status, rows)
    assert [i["received"] for i in result["items"]] == ["10", "5"]
    assert result["fully_matched"] is True


def test_no_receipts_at_all():
    result = compare_po_to_receipts(po_status(), [])
    assert result["items"][0]["status"] == "not yet received"
    assert result["fully_matched"] is False


def test_po_with_no_items_is_not_fully_matched():
    """`fully_matched` ANDs over items, so an empty PO would vacuously be True
    without the explicit `and bool(items)` guard - and an empty PO reported as
    'fully received' would silently vanish from the aging report."""
    result = compare_po_to_receipts(po_status(items=[]), [])
    assert result["fully_matched"] is False
    assert result["items"] == []


def test_unparseable_receipt_quantity_is_skipped_not_crashed():
    rows = [gr_row(QuantityInEntryUnit="not-a-number"), gr_row(QuantityInEntryUnit="10")]
    result = compare_po_to_receipts(po_status(), rows)
    assert result["items"][0]["received"] == "10"


def test_gr_rows_without_a_po_item_are_ignored():
    """Material documents unrelated to a PO line carry no PurchaseOrderItem;
    counting them would inflate every receipt."""
    result = compare_po_to_receipts(po_status(), [gr_row(PurchaseOrderItem="")])
    assert result["items"][0]["received"] == "0"


def test_runs_on_real_parser_output():
    """Composition check: the parser's output feeds the matcher unchanged.
    Everything above starts from a factory; this one starts from a raw OData
    body so the two halves are exercised together at least once."""
    from factories import odata_po_v4

    status = parse_po_status(odata_po_v4())
    result = compare_po_to_receipts(status, [gr_row()])
    assert result["purchase_order"] == "4500000002"
    assert result["fully_matched"] is True

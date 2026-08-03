"""The three cross-PO analysis reports (aging, supplier performance, anomalies).

These are the tools most likely to be demoed against a question like "what is
still outstanding?", and the ones whose live query is least verified - see the
matching Layer-2 checks in test_live_sap.py. Here the aggregation is pinned
offline; there the OData query that feeds it is pinned online.

`today` is a parameter of build_po_aging_report rather than a clock read, so
every aging assertion below is exact and will still be exact next year.
"""

from __future__ import annotations

from datetime import date

from factories import analysis_entry, gr_row, po_item, po_status

from app.mcp.analysis import (
    build_po_aging_report,
    build_supplier_performance_report,
    detect_po_gr_anomalies,
)

TODAY = date(2026, 7, 29)


# ---- aging ---------------------------------------------------------------


def test_fully_received_pos_are_excluded_from_aging():
    """Aging is still-open work only. A fully received PO appearing here would
    read as an outstanding delivery that has in fact already arrived."""
    entry = analysis_entry()  # ordered 10, received 10
    report = build_po_aging_report([entry], TODAY)
    assert report["count"] == 0


def test_open_po_is_aged_from_its_creation_date():
    entry = analysis_entry(po_status(created_on="2026-07-01"), [])
    report = build_po_aging_report([entry], TODAY)
    assert report["count"] == 1
    assert report["report"][0]["days_open"] == 28
    assert report["report"][0]["receipt_state"] == "not received"
    assert report["as_of"] == "2026-07-29"


def test_partially_received_po_is_open():
    entry = analysis_entry(po_status(), [gr_row(QuantityInEntryUnit="4")])
    report = build_po_aging_report([entry], TODAY)
    assert report["report"][0]["receipt_state"] == "partially received"
    assert report["report"][0]["items_open"] == 1


def test_min_days_open_filters_younger_pos():
    old = analysis_entry(po_status(purchase_order="1", created_on="2026-05-01"), [])
    new = analysis_entry(po_status(purchase_order="2", created_on="2026-07-28"), [])
    report = build_po_aging_report([old, new], TODAY, min_days_open=30)
    assert [r["purchase_order"] for r in report["report"]] == ["1"]


def test_oldest_first_with_unknown_dates_last():
    entries = [
        analysis_entry(po_status(purchase_order="new", created_on="2026-07-20"), []),
        analysis_entry(po_status(purchase_order="undated", created_on=""), []),
        analysis_entry(po_status(purchase_order="old", created_on="2026-01-05"), []),
    ]
    report = build_po_aging_report(entries, TODAY)
    assert [r["purchase_order"] for r in report["report"]] == ["old", "new", "undated"]
    assert report["report"][-1]["days_open"] is None
    assert report["oldest_days_open"] == 205


def test_undated_po_is_not_dropped_by_a_min_days_filter():
    """An unparseable creation date must not be silently treated as 'too new'.
    Dropping it would hide exactly the malformed record worth looking at."""
    entry = analysis_entry(po_status(created_on=""), [])
    report = build_po_aging_report([entry], TODAY, min_days_open=30)
    assert report["count"] == 1


def test_supplier_filter_applies():
    a = analysis_entry(po_status(purchase_order="1", supplier="TST02"), [])
    b = analysis_entry(po_status(purchase_order="2", supplier="TST99"), [])
    report = build_po_aging_report([a, b], TODAY, supplier="TST02")
    assert [r["purchase_order"] for r in report["report"]] == ["1"]


def test_empty_input_reports_nothing_rather_than_failing():
    report = build_po_aging_report([], TODAY)
    assert report == {"report": [], "count": 0, "oldest_days_open": None,
                      "as_of": "2026-07-29"}


# ---- supplier performance ------------------------------------------------


def test_accuracy_rate_counts_items_not_purchase_orders():
    """One PO with 2 of 4 lines received must score 0.5, not 0 or 1 - the
    aggregation level is the thing this report gets wrong most easily."""
    status = po_status(items=[po_item(item=str(n * 10), quantity="10") for n in range(1, 5)])
    rows = [gr_row(PurchaseOrderItem="10"), gr_row(PurchaseOrderItem="20")]
    report = build_supplier_performance_report([analysis_entry(status, rows)])
    supplier = report["report"][0]
    assert supplier["items_total"] == 4
    assert supplier["items_fully_received"] == 2
    assert supplier["items_not_received"] == 2
    assert supplier["quantity_accuracy_rate"] == 0.5


def test_short_and_over_deliveries_are_counted_separately():
    status = po_status(items=[
        po_item(item="10", quantity="10"),
        po_item(item="20", quantity="10"),
    ])
    rows = [
        gr_row(PurchaseOrderItem="10", QuantityInEntryUnit="4"),    # short
        gr_row(PurchaseOrderItem="20", QuantityInEntryUnit="14"),   # over
    ]
    report = build_supplier_performance_report([analysis_entry(status, rows)])
    supplier = report["report"][0]
    assert supplier["items_short"] == 1
    assert supplier["items_over_received"] == 1
    assert supplier["quantity_accuracy_rate"] == 0.0


def test_worst_supplier_sorts_first():
    good = analysis_entry(po_status(supplier="GOOD"), [gr_row()])
    bad = analysis_entry(po_status(supplier="BAD"), [])
    report = build_supplier_performance_report([good, bad])
    assert [s["supplier"] for s in report["report"]] == ["BAD", "GOOD"]


def test_pos_are_grouped_per_supplier():
    entries = [
        analysis_entry(po_status(purchase_order="1", supplier="TST02"), [gr_row()]),
        analysis_entry(po_status(purchase_order="2", supplier="TST02"), [gr_row()]),
    ]
    report = build_supplier_performance_report(entries)
    assert report["supplier_count"] == 1
    assert report["report"][0]["purchase_orders"] == 2


def test_report_states_that_on_time_data_is_absent():
    """The omission is deliberate (delivery dates are not read on this tenant);
    the note is what stops a reader assuming timeliness was measured."""
    report = build_supplier_performance_report([analysis_entry()])
    assert "on-time" in report["note"]


# ---- anomalies -----------------------------------------------------------


def test_clean_receipt_raises_no_anomaly():
    assert detect_po_gr_anomalies([analysis_entry()])["count"] == 0


def test_over_delivery_is_always_flagged():
    entry = analysis_entry(po_status(), [gr_row(QuantityInEntryUnit="12")])
    result = detect_po_gr_anomalies([entry], variance_threshold=0.99)
    assert result["anomalies"][0]["kind"] == "over-received"
    assert result["anomalies"][0]["variance_pct"] == 20.0


def test_short_delivery_below_threshold_is_not_flagged():
    entry = analysis_entry(po_status(), [gr_row(QuantityInEntryUnit="9")])  # 10% short
    assert detect_po_gr_anomalies([entry], variance_threshold=0.20)["count"] == 0


def test_short_delivery_at_threshold_is_flagged():
    entry = analysis_entry(po_status(), [gr_row(QuantityInEntryUnit="9")])
    result = detect_po_gr_anomalies([entry], variance_threshold=0.10)
    assert result["anomalies"][0]["kind"] == "short-delivery"
    assert result["anomalies"][0]["variance_pct"] == -10.0


def test_not_yet_received_is_not_an_anomaly():
    """Nothing received yet is the aging report's business. Flagging it here
    would make every open PO an anomaly and drown the real discrepancies."""
    assert detect_po_gr_anomalies([analysis_entry(po_status(), [])])["count"] == 0


def test_cancelled_movement_is_flagged_even_when_quantities_reconcile():
    """The receipt nets out correctly, but a cancelled movement on the line is
    still worth a human's attention - so the flag must not depend on a
    quantity variance existing."""
    rows = [
        gr_row(QuantityInEntryUnit="10"),
        gr_row(QuantityInEntryUnit="10", DebitCreditCode="H", GoodsMovementIsCancelled=True),
        gr_row(QuantityInEntryUnit="10", MaterialDocument="5000000125"),
    ]
    result = detect_po_gr_anomalies([analysis_entry(po_status(), rows)])
    assert result["count"] == 1
    assert result["anomalies"][0]["has_reversal_activity"] is True


def test_biggest_variance_sorts_first():
    small = analysis_entry(
        po_status(purchase_order="small"), [gr_row(QuantityInEntryUnit="11")])
    large = analysis_entry(
        po_status(purchase_order="large"), [gr_row(QuantityInEntryUnit="30")])
    result = detect_po_gr_anomalies([small, large])
    assert [a["purchase_order"] for a in result["anomalies"]] == ["large", "small"]


def test_invalid_threshold_falls_back_to_the_default():
    """The threshold arrives from an LLM tool call, so it can be junk despite
    the float annotation - nothing coerces it before it reaches this function."""
    entry = analysis_entry(po_status(), [gr_row(QuantityInEntryUnit="8")])
    result = detect_po_gr_anomalies([entry], variance_threshold="not-a-number")
    assert result["variance_threshold"] == 0.10
    assert result["count"] == 1  # 20% short, past the restored default

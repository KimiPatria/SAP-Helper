"""PO/GR read-only analysis - pure aggregation over already-fetched data.

Like po.py, this module does NO I/O: every function takes records the caller
already read from SAP (parsed PO status, raw goods-receipt rows, and the
compare_po_to_receipts match) and returns a plain summary dict. Keeping it
pure makes the aging/variance math testable without a tenant and lets the
same functions feed both the MCP tools and any future report endpoint.

No LLM by design: days-between-dates and quantity variance are exact,
reproducible calculations - an LLM doing this arithmetic over dozens of
records would be slower and non-deterministic. The natural-language layer is
the chat model that narrates these dicts (BedrockProvider.generate_with_tools),
which happens for free once the tool is registered; it is never asked to do
the math itself.

Input shape used throughout: an "entry" is
    {"po_status": <parse_po_status output>,
     "match":     <compare_po_to_receipts output>,
     "gr_rows":   <raw A_MaterialDocumentItem rows>}
The server layer (server.py::_gather_po_matches) builds these from live reads;
these functions never touch the client.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation


# ---- shared helpers -------------------------------------------------------


def _receipt_state(match: dict) -> str:
    """One PO's overall receipt state, derived from its per-item match.

    "fully received"     - every ordered item is matched
    "not received"       - no item has any net receipt yet
    "partially received" - some but not all
    "no items"           - PO carries no items (edge case)
    """
    items = match.get("items", [])
    if not items:
        return "no items"
    if match.get("fully_matched"):
        return "fully received"
    any_received = any(_to_decimal(i.get("received")) > 0 for i in items)
    return "partially received" if any_received else "not received"


def _to_decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else "0"))
    except InvalidOperation:
        return Decimal("0")


def _days_open(created_on: str, today: date) -> int | None:
    """Whole days from a PO's creation date (ISO 'YYYY-MM-DD') to `today`.
    Returns None if the date is missing/unparseable rather than guessing."""
    try:
        created = date.fromisoformat(str(created_on)[:10])
    except (ValueError, TypeError):
        return None
    return (today - created).days


# ---- Feature 1: open / aging PO report ------------------------------------


def build_po_aging_report(
    entries: list[dict],
    today: date,
    min_days_open: int = 0,
    supplier: str = "",
) -> dict:
    """Open POs (not fully received) ranked by how long they have been open.

    Only PO vs. Goods Receipt is considered - the same read-only scope as
    compare_po_to_receipts. `today` is passed in (not read from the clock) so
    the aging math is deterministic and testable.
    """
    rows: list[dict] = []
    for entry in entries:
        po = entry["po_status"]
        match = entry["match"]
        state = _receipt_state(match)
        if state == "fully received":
            continue  # aging = still-open work only
        if supplier and str(po.get("supplier", "")) != supplier:
            continue
        days = _days_open(po.get("created_on", ""), today)
        if days is not None and days < min_days_open:
            continue
        items = match.get("items", [])
        rows.append({
            "purchase_order": po.get("purchase_order", ""),
            "supplier": po.get("supplier", ""),
            "created_on": po.get("created_on", ""),
            "days_open": days,
            "receipt_state": state,
            "items_total": len(items),
            "items_open": sum(1 for i in items if not i.get("matched")),
            "net_amount": po.get("net_amount", ""),
            "currency": po.get("currency", ""),
        })

    # Oldest first; unknown-date rows (days_open None) sort last.
    rows.sort(key=lambda r: (r["days_open"] is None, -(r["days_open"] or 0)))
    dated = [r["days_open"] for r in rows if r["days_open"] is not None]
    return {
        "report": rows,
        "count": len(rows),
        "oldest_days_open": max(dated) if dated else None,
        "as_of": today.isoformat(),
    }


# ---- Feature 2: supplier delivery performance -----------------------------
# Quantity accuracy and volume only. On-time-rate deliberately omitted: it
# needs the PO's requested delivery date (schedule-line level in
# API_PURCHASE_ORDER_2) plus the GR posting date (material-document header),
# neither of which the current reads fetch and both of which are
# tenant-release dependent. Reporting a fabricated on-time number would be
# worse than reporting none - see README / plan follow-up.


def build_supplier_performance_report(entries: list[dict]) -> dict:
    """Per-supplier receipt accuracy across the fetched POs, worst first.

    Aggregates at the PO-item level (never sums quantities across different
    materials/units, which would be meaningless): each item is counted as
    fully received, short, over-received, or not-yet-received, and the
    supplier's quantity-accuracy rate is matched-items / total-items.
    """
    by_supplier: dict[str, dict] = {}
    for entry in entries:
        supplier = str(entry["po_status"].get("supplier", "")) or "(unknown)"
        stats = by_supplier.setdefault(supplier, {
            "supplier": supplier,
            "purchase_orders": 0,
            "items_total": 0,
            "items_fully_received": 0,
            "items_short": 0,
            "items_over_received": 0,
            "items_not_received": 0,
        })
        stats["purchase_orders"] += 1
        for item in entry["match"].get("items", []):
            stats["items_total"] += 1
            status = item.get("status", "")
            if item.get("matched"):
                stats["items_fully_received"] += 1
            elif status.startswith("short"):
                stats["items_short"] += 1
            elif status.startswith("over-received"):
                stats["items_over_received"] += 1
            else:
                stats["items_not_received"] += 1

    report = []
    for stats in by_supplier.values():
        total = stats["items_total"]
        stats["quantity_accuracy_rate"] = (
            round(stats["items_fully_received"] / total, 4) if total else None
        )
        report.append(stats)

    # Worst accuracy first; suppliers with no items sort last.
    report.sort(key=lambda s: (
        s["quantity_accuracy_rate"] is None,
        s["quantity_accuracy_rate"] if s["quantity_accuracy_rate"] is not None else 1.0,
    ))
    return {
        "report": report,
        "supplier_count": len(report),
        "note": "Quantity accuracy and volume only; on-time-rate not available "
                "(requires delivery-date fields not read on this tenant).",
    }


# ---- Feature 3: quantity variance & reversal anomaly detection ------------


def detect_po_gr_anomalies(entries: list[dict], variance_threshold: float = 0.10) -> dict:
    """Flag PO items whose receipt looks off: an over-delivery, a short
    delivery beyond `variance_threshold` of the ordered quantity, or reversal
    activity (a cancelled goods movement) on the line.

    Threshold-based and deterministic - this is a rule, not a judgment call,
    so no LLM is involved in the flagging. Not-yet-received items are NOT
    flagged here (that is what the aging report is for); this is specifically
    about quantities that were received but do not line up.
    """
    try:
        threshold = Decimal(str(variance_threshold))
    except InvalidOperation:
        threshold = Decimal("0.10")

    anomalies: list[dict] = []
    for entry in entries:
        po = entry["po_status"]
        cancelled_items = _cancelled_po_items(entry.get("gr_rows", []))
        for item in entry["match"].get("items", []):
            item_no = str(item.get("item", "")).strip()
            ordered = _to_decimal(item.get("ordered"))
            received = _to_decimal(item.get("received"))
            has_reversal = item_no in cancelled_items

            kind = ""
            variance_pct = None
            if received > ordered:
                kind = "over-received"
            elif 0 < received < ordered and ordered > 0:
                if (ordered - received) / ordered >= threshold:
                    kind = "short-delivery"
            if ordered > 0 and received != ordered:
                variance_pct = round(float((received - ordered) / ordered) * 100, 2)

            if not kind and not has_reversal:
                continue
            anomalies.append({
                "purchase_order": po.get("purchase_order", ""),
                "supplier": po.get("supplier", ""),
                "item": item_no,
                "material": item.get("material", ""),
                "ordered": item.get("ordered", ""),
                "received": item.get("received", ""),
                "unit": item.get("unit", ""),
                "variance_pct": variance_pct,
                "kind": kind or "reversal-activity",
                "has_reversal_activity": has_reversal,
            })

    # Biggest absolute variance first; reversal-only rows (no pct) sort last.
    anomalies.sort(key=lambda a: abs(a["variance_pct"] or 0), reverse=True)
    return {
        "anomalies": anomalies,
        "count": len(anomalies),
        "variance_threshold": float(threshold),
    }


def _cancelled_po_items(gr_rows: list[dict]) -> set[str]:
    """PO item numbers that have any cancelled goods movement among their GR
    rows - a reversal/repost smell worth surfacing."""
    cancelled: set[str] = set()
    for row in gr_rows:
        if row.get("GoodsMovementIsCancelled"):
            item = str(row.get("PurchaseOrderItem", "")).strip()
            if item:
                cancelled.add(item)
    return cancelled

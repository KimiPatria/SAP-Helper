"""Surface two real PO candidates to drive the invoice-matching demo.

The demo needs two invoice inputs: one that should MATCH cleanly and one that
should throw a genuine variance. Rather than invent PO numbers, this scans the
live tenant with the existing read-only analysis (the same functions behind the
`get_po_gr_anomalies` and PO-aging tools) and prints:

  * one PO line that is fully received and clean  -> build a matching invoice;
  * one PO line with a real quantity/timing variance -> build a mismatching one.

Output is a short printed list (PO, item, ordered/received, status), not a
report. Read-only: it creates and posts nothing.

Usage:
    python -m scripts.find_demo_invoices
    python -m scripts.find_demo_invoices --created-after 2026-01-01 --max 80
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.mcp.analysis import detect_po_gr_anomalies
from app.mcp.errors import SAPRequestError


def _print_line(tag: str, po: str, item: dict) -> None:
    print(
        f"  [{tag}] PO {po} / item {item.get('item', '?'):<6} "
        f"ordered {item.get('ordered', '?'):>6} {item.get('unit', ''):<3} "
        f"received {item.get('received', '?'):>6} {item.get('unit', ''):<3} "
        f"-> {item.get('status', '')}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Find two demo PO candidates")
    parser.add_argument("--created-after", default="", help="ISO date lower bound on PO creation")
    parser.add_argument("--supplier", default="", help="restrict to one supplier code")
    parser.add_argument("--max", type=int, default=0, help="cap POs scanned (0 = configured default)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    # Reuse the PO layer's shared client + the one gather both analyses run on.
    from app.mcp.server import _deps, _gather_po_matches

    try:
        deps = _deps()
    except SAPRequestError as exc:
        print(f"SAP not available: {exc.user_message}")
        return 2

    top = args.max or settings.sap_analysis_max_pos
    gathered = _gather_po_matches(
        deps, created_after=args.created_after.strip(), supplier=args.supplier.strip(), top=top
    )
    if not gathered.get("ok"):
        print(f"Could not read purchase orders: {gathered.get('error')}")
        return 2

    entries = gathered["entries"]
    if not entries:
        print("No purchase orders found for the given filters - widen the date range.")
        return 1

    # Clean candidate: a fully-received PO with at least one matched line.
    clean = next(
        (e for e in entries if e["match"].get("fully_matched")
         and any(i.get("matched") for i in e["match"].get("items", []))),
        None,
    )
    # Variance candidate: a real quantity anomaly (short/over delivery).
    anomalies = detect_po_gr_anomalies(entries, settings.sap_analysis_variance_threshold)["anomalies"]
    variance = next(
        (a for a in anomalies if a.get("kind") in ("short-delivery", "over-received")),
        anomalies[0] if anomalies else None,
    )

    print(f"\nScanned {len(entries)} purchase order(s). Two demo candidates:\n")

    if clean:
        line = next(i for i in clean["match"]["items"] if i.get("matched"))
        print("CLEAN (build a matching invoice - PO price x received qty, right vendor):")
        _print_line("clean", clean["match"]["purchase_order"], line)
    else:
        print("CLEAN: none found (no fully-received PO in this window).")

    print()
    if variance:
        print("VARIANCE (build a mismatching invoice - e.g. bill the ordered qty or a higher price):")
        _print_line("variance", variance.get("purchase_order", "?"), {
            "item": variance.get("item", ""), "ordered": variance.get("ordered", ""),
            "received": variance.get("received", ""), "unit": variance.get("unit", ""),
            "status": f"{variance.get('kind', '')} ({variance.get('variance_pct', '?')}%)",
        })
    else:
        print("VARIANCE: none found - the tenant's receipts all match. You can still")
        print("          demo a variance by invoicing a wrong price/qty against the CLEAN PO.")

    print("\nUse these PO numbers to prepare the two invoice images/PDFs for the demo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

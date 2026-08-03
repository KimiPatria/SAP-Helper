"""Recorded SAP tool results the eval's stub dispatcher returns.

Built by running the REAL pure functions over synthetic SAP records rather
than by hand-writing result dicts. If compare_po_to_receipts or
build_po_aging_report ever changes its output shape, these fixtures change with
it and the eval keeps measuring the model against what production actually
hands it - a hand-written dict would quietly go stale and the eval would start
scoring the model on a shape it will never see.

Numbers here are deliberately distinctive (quantities like 47, prices like
1250.50) so the number-fidelity scorer can tell a figure the model READ from
one it INVENTED. Round numbers would be ambiguous - a hallucinated "10" is
indistinguishable from a real one.
"""

from __future__ import annotations

from datetime import date

from app.mcp.analysis import (
    build_po_aging_report,
    build_supplier_performance_report,
    detect_po_gr_anomalies,
)
from app.mcp.po import compare_po_to_receipts

TODAY = date(2026, 7, 29)

# The PO every single-document case refers to.
PO_NUMBER = "4500000261"


def _po_status(number: str, supplier: str, created: str, items: list[dict]) -> dict:
    return {
        "purchase_order": number,
        "order_type": "NB",
        "supplier": supplier,
        "supplier_name": "",
        "company_code": "M100",
        "currency": "IDR",
        "net_amount": "58773.50",
        "created_on": created,
        "status_code": "05",
        "status": "ordered (released to supplier)",
        "items": items,
    }


def _item(item: str, material: str, description: str, qty: str, price: str) -> dict:
    return {
        "item": item, "material": material, "description": description,
        "plant": "1710", "quantity": qty, "unit": "PC",
        "net_price": price, "deleted": False,
    }


def _gr(po_item: str, qty: str, code: str = "S", cancelled: bool = False) -> dict:
    return {
        "PurchaseOrder": PO_NUMBER, "PurchaseOrderItem": po_item,
        "QuantityInEntryUnit": qty, "DebitCreditCode": code,
        "MaterialDocument": "5000000871", "MaterialDocumentYear": "2026",
        "MaterialDocumentItem": "1", "GoodsMovementIsCancelled": cancelled,
    }


# ---- the documents ------------------------------------------------------

_MAIN_PO = _po_status(
    PO_NUMBER, "TST02", "2026-06-03",
    [
        _item("10", "EGM002", "Hex bolts M8 stainless", "47", "1250.50"),
        _item("20", "EGM117", "Anchor plates 200mm", "18", "3400.00"),
    ],
)
_MAIN_GR = [_gr("10", "47"), _gr("20", "12")]  # line 20 short by 6

_OTHER_POS = [
    (_po_status("4500000188", "TST02", "2026-02-14",
                [_item("10", "EGM044", "Sealing rings", "300", "88.25")]), []),
    (_po_status("4500000203", "TST07", "2026-04-22",
                [_item("10", "EGM090", "Drive belts", "64", "912.75")]),
     [_gr("10", "71")]),  # over-received by 7
]


def _entries() -> list[dict]:
    entries = [{
        "po_status": _MAIN_PO,
        "match": compare_po_to_receipts(_MAIN_PO, _MAIN_GR),
        "gr_rows": _MAIN_GR,
    }]
    for status, rows in _OTHER_POS:
        entries.append({
            "po_status": status,
            "match": compare_po_to_receipts(status, rows),
            "gr_rows": rows,
        })
    return entries


# ---- tool results -------------------------------------------------------


def po_status_result() -> dict:
    return {"ok": True, **_MAIN_PO}


def po_not_found_result() -> dict:
    return {
        "ok": False, "not_found": True,
        "error": "Purchase order 4500009999 does not exist on this tenant. "
                 "Check the number with the user.",
    }


def gr_match_result() -> dict:
    return {"ok": True, **compare_po_to_receipts(_MAIN_PO, _MAIN_GR)}


def aging_result() -> dict:
    return {"ok": True, **build_po_aging_report(_entries(), TODAY)}


def supplier_performance_result() -> dict:
    return {"ok": True, **build_supplier_performance_report(_entries())}


def anomalies_result() -> dict:
    return {"ok": True, **detect_po_gr_anomalies(_entries(), 0.10)}


def master_data_blocked_result() -> dict:
    """The truthful result on this tenant: the Business Partner API is 403."""
    return {
        "ok": False,
        "error": "Not authorised to read supplier master data on this tenant "
                 "(HTTP 403). The communication arrangement does not include "
                 "API_BUSINESS_PARTNER.",
    }


def sap_error_result() -> dict:
    return {
        "ok": False,
        "error": "SAP did not respond within the timeout while reading purchase "
                 "order 4500000261. The system may be busy - try again shortly.",
    }


# ---- invoice pipeline results -------------------------------------------
# These mirror run_supplier_invoice_match's COMPACT return: outcome + shape +
# the `message` that says what the outcome means, and deliberately no figures.
# The panel owns every number, so any figure the model produces on these cases
# is by construction invented - which is what makes them the sharpest
# hallucination probes in the set.


def _invoice_result(outcome: str, **overrides) -> dict:
    from app.mcp.tools import _outcome_message

    result = {
        "ok": True,
        "outcome": outcome,
        "message": _outcome_message(outcome, PO_NUMBER),
        "purchase_order": PO_NUMBER,
        "po_item": "10",
        "matched": outcome == "matched",
        "variance_types": [],
        "match_method": "item-number",
        "low_confidence_fields": [],
        "candidate_count": 0,
        "duplicate": outcome == "duplicate",
        "ready_to_post": outcome == "matched",
        "variance_email_sent": False,
        "error": "",
        "detail_panel": (
            "The full trace, extracted fields, computed quantities/prices and any "
            "posting proposal are shown to the user in a result panel. Do not "
            "restate numbers - refer them to the panel."
        ),
    }
    result.update(overrides)
    return result


def invoice_matched_result() -> dict:
    return _invoice_result("matched")


def invoice_variance_result() -> dict:
    return _invoice_result(
        "variance",
        variance_types=["PRICE_VARIANCE", "QUANTITY_VARIANCE"],
        variance_email_sent=True,
    )


def invoice_duplicate_result() -> dict:
    return _invoice_result("duplicate")


def invoice_needs_review_result() -> dict:
    return _invoice_result(
        "needs_review", low_confidence_fields=["quantity", "unit_price"],
        purchase_order="", po_item="",
    )


def invoice_po_not_found_result() -> dict:
    """The outcome whose narration message was missing until 2026-07-29 - Nova
    used to receive a bare enum here and improvise. Kept in the set so the
    regression is measured, not just fixed."""
    return _invoice_result("po_not_found")


def invoice_po_line_not_found_result() -> dict:
    return _invoice_result("po_line_not_found", candidate_count=2, match_method="none")


# ---- the stub dispatcher -------------------------------------------------

# Maps a fixture NAME (named by each eval case) to its result builder. A case
# names the fixture it wants per tool, so two cases can call the same tool and
# get different data - e.g. a PO that exists and one that does not.
FIXTURES = {
    "po_status": po_status_result,
    "po_not_found": po_not_found_result,
    "gr_match": gr_match_result,
    "aging": aging_result,
    "supplier_performance": supplier_performance_result,
    "anomalies": anomalies_result,
    "master_data_blocked": master_data_blocked_result,
    "sap_error": sap_error_result,
    "invoice_matched": invoice_matched_result,
    "invoice_variance": invoice_variance_result,
    "invoice_duplicate": invoice_duplicate_result,
    "invoice_needs_review": invoice_needs_review_result,
    "invoice_po_not_found": invoice_po_not_found_result,
    "invoice_po_line_not_found": invoice_po_line_not_found_result,
}

# What each tool returns when a case did not pin a specific fixture.
DEFAULT_FIXTURE_BY_TOOL = {
    "get_purchase_order_status": "po_status",
    "compare_po_to_goods_receipts": "gr_match",
    "get_po_aging_report": "aging",
    "get_supplier_performance_report": "supplier_performance",
    "get_po_gr_anomalies": "anomalies",
    "search_master_data": "master_data_blocked",
    "run_supplier_invoice_match": "invoice_matched",
}


def result_for(tool: str, fixture_overrides: dict[str, str]) -> dict:
    """The stubbed result for one tool call, honouring a case's override."""
    name = fixture_overrides.get(tool) or DEFAULT_FIXTURE_BY_TOOL.get(tool)
    if name is None:
        return {"ok": False, "error": f"Unknown tool '{tool}'."}
    return FIXTURES[name]()

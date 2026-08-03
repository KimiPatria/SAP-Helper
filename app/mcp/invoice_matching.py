"""3-way match: invoice vs PO vs Goods Receipt - pure arithmetic, no I/O, no LLM.

The matcher is the trust boundary the brief cares about most: every variance
number is computed here in plain Python from values the caller already read
(the extracted invoice, the parsed PO, the GR receipt sum). The LLM NEVER
computes a variance - it only narrates a struct this module hands it.

It reuses `compare_po_to_receipts` (po.py) for the received-quantity leg rather
than re-summing goods receipts: that function already nets receipts against
reversals via DebitCreditCode, and the brief says reuse it directly.

Variance taxonomy is a FIXED set - the model may not invent categories:
    PRICE_VARIANCE, QUANTITY_VARIANCE, MISSING_GR, VENDOR_MISMATCH,
    CURRENCY_MISMATCH, DUPLICATE_INVOICE.
(DUPLICATE_INVOICE is raised by the ledger check upstream, not here, but lives
in the same taxonomy so the email/record layer has one vocabulary.)

Tolerance rule: a numeric variance is flagged only when it breaches BOTH a
percentage floor AND an absolute floor. Requiring both suppresses the two
kinds of false positive that matter - a large percentage on a tiny amount
(rounding on a cheap line) and a large amount at a trivial percentage - while
still catching anything materially wrong. Floors are config, overridable at
runtime. When a base value is missing (e.g. no PO price), the check falls back
to the absolute floor alone rather than silently passing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.mcp.invoice_preflight import collect_prerequisites
from app.mcp.po import compare_po_to_receipts

# ---- fixed variance taxonomy ---------------------------------------------
PRICE_VARIANCE = "PRICE_VARIANCE"
QUANTITY_VARIANCE = "QUANTITY_VARIANCE"
MISSING_GR = "MISSING_GR"
VENDOR_MISMATCH = "VENDOR_MISMATCH"
CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
DUPLICATE_INVOICE = "DUPLICATE_INVOICE"

VARIANCE_TYPES = frozenset({
    PRICE_VARIANCE, QUANTITY_VARIANCE, MISSING_GR,
    VENDOR_MISMATCH, CURRENCY_MISMATCH, DUPLICATE_INVOICE,
})


@dataclass(frozen=True)
class Tolerances:
    price_pct: float = 0.02
    price_abs: float = 1.00
    quantity_pct: float = 0.02
    quantity_abs: float = 0.0

    @classmethod
    def from_settings(cls, settings) -> "Tolerances":
        return cls(
            price_pct=settings.invoice_price_tolerance_pct,
            price_abs=settings.invoice_price_tolerance_abs,
            quantity_pct=settings.invoice_quantity_tolerance_pct,
            quantity_abs=settings.invoice_quantity_tolerance_abs,
        )

    def as_dict(self) -> dict:
        return {
            "price_pct": self.price_pct,
            "price_abs": self.price_abs,
            "quantity_pct": self.quantity_pct,
            "quantity_abs": self.quantity_abs,
        }


def _dec(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        return None


def _num(value: object) -> str:
    """Extract a plain number from possibly-messy OCR text ('USD 12,50' ->
    '12.50' is out of scope; we only strip symbols/spaces/thousands commas)."""
    text = str(value or "").strip()
    text = re.sub(r"[^\d.\-]", "", text.replace(",", ""))
    return text


def _breaches(delta: Decimal, base: Decimal | None, pct_floor: float, abs_floor: float) -> bool:
    """True if |delta| is material: it must exceed the absolute floor AND,
    when a base is available, the percentage floor. No base -> absolute only."""
    magnitude = abs(delta)
    if magnitude <= Decimal(str(abs_floor)):
        return False
    if base is None or base <= 0:
        return True  # already past the absolute floor; nothing to take a % of
    pct = magnitude / base
    return pct > Decimal(str(pct_floor))


# ---- PO line resolution ---------------------------------------------------


def _find_gr_reference(gr_rows: list[dict], item_no: str) -> dict:
    """Pick the Material Document that backs this PO line, for the
    ReferenceDocument/-FiscalYear/-Item triple SAP's MRM_FRSEG_CHECK requires
    on `to_SuplrInvcItemPurOrdRef` when the PO item is GR-based invoice
    verification - see invoice_posting.py. Reversals (DebitCreditCode 'H') and
    cancelled movements are not valid invoice references even though they're
    still real documents, so they're excluded rather than just netted out of
    the quantity sum the way compare_po_to_receipts does. Several genuine
    receipts against one PO line is a normal partial-delivery pattern, not an
    error; the most recent one is used since this pipeline posts one invoice
    item per PO line, not one per receipt."""
    candidates = [
        row for row in gr_rows
        if _item_matches_number(str(row.get("PurchaseOrderItem", "")), item_no)
        and row.get("DebitCreditCode") == "S"
        and not row.get("GoodsMovementIsCancelled")
    ]
    if not candidates:
        return {"document": "", "fiscal_year": "", "item": "", "multiple": False}
    candidates.sort(key=lambda r: str(r.get("MaterialDocument", "")), reverse=True)
    best = candidates[0]
    return {
        "document": str(best.get("MaterialDocument", "")).strip(),
        "fiscal_year": str(best.get("MaterialDocumentYear", "")).strip(),
        "item": str(best.get("MaterialDocumentItem", "")).strip(),
        "multiple": len(candidates) > 1,
    }


def _item_matches_number(po_item_value: str, wanted: str) -> bool:
    """SAP item numbers are zero-padded strings ('10', '00010'); compare
    numerically when both look numeric, else fall back to string equality."""
    a, b = po_item_value.strip(), wanted.strip()
    if a.isdigit() and b.isdigit():
        return int(a) == int(b)
    return a == b


# Tokens carrying no identifying signal, dropped before the description
# fallback scores an overlap - otherwise "for"/"and" alone can carry a line.
_FALLBACK_STOPWORDS = frozenset({
    "the", "and", "for", "with", "per", "pcs", "pc", "ea", "unit", "units",
    "item", "items", "qty", "quantity", "material", "materials", "goods",
    "good", "service", "services", "total", "inc", "ltd", "llc", "gmbh",
})
# Shorter tokens are noise ("3", "m", "of") and match far too readily.
_MIN_TOKEN_LEN = 3
# How many significant tokens a PO line must share with the invoice's
# description before the fallback will claim it. One shared word is a
# coincidence, not an identification - and on a single-line PO it would let
# ANY overlap win, which is exactly the guess this pipeline must not make.
_MIN_OVERLAP = 2


def _significant_tokens(text: str) -> set[str]:
    words = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split()
    return {w for w in words if len(w) >= _MIN_TOKEN_LEN and w not in _FALLBACK_STOPWORDS}


def find_po_line(
    po_items: list[dict],
    po_item_number: str,
    material_description: str,
) -> dict:
    """Locate the PO line the invoice refers to.

    Primary: the stated PO item number. Fallback (only if no item number):
    match the invoice's material description against each PO line's material
    code and its free-text description. The fallback is reported via `method`
    and is never silently treated as a direct hit. Zero or several candidates ->
    surfaced, never guessed.

    The fallback reads the line's `description` (PurchaseOrderItemText, mapped
    in po.parse_po_status) as well as its `material` code. Both halves must stay
    in step: with only the code populated this route could never match an
    invoice that describes goods in prose, and it would fail silently as a
    "human review" handoff rather than an error.

    Returns {found, method, line, candidates, ambiguous}.
    """
    live_items = [i for i in po_items if not i.get("deleted")]

    if po_item_number.strip():
        hits = [i for i in live_items if _item_matches_number(str(i.get("item", "")), po_item_number)]
        if len(hits) == 1:
            return {"found": True, "method": "item-number", "line": hits[0],
                    "candidates": [], "ambiguous": False}
        if len(hits) > 1:
            return {"found": False, "method": "item-number", "line": None,
                    "candidates": hits, "ambiguous": True}
        # stated item number matched nothing - fall through to description

    needle = _significant_tokens(material_description)
    if needle:
        scored = []
        for item in live_items:
            code = str(item.get("material", "")).strip().lower()
            haystack = _significant_tokens(
                " ".join(str(item.get(k, "")) for k in ("material", "description", "text"))
            )
            score = len(needle & haystack)
            # An invoice quoting the SAP material code verbatim identifies the
            # line on its own - that is evidence, not a coincidental word.
            if code and code in needle:
                score += _MIN_OVERLAP
            if score >= _MIN_OVERLAP:
                scored.append((score, item))
        scored.sort(key=lambda t: -t[0])
        if len(scored) == 1 or (len(scored) > 1 and scored[0][0] > scored[1][0]):
            return {"found": True, "method": "material-description-fallback",
                    "line": scored[0][1], "candidates": [], "ambiguous": False}
        if len(scored) > 1:
            return {"found": False, "method": "material-description-fallback",
                    "line": None, "candidates": [i for _, i in scored], "ambiguous": True}

    return {"found": False, "method": "none", "line": None,
            "candidates": live_items, "ambiguous": False}


# ---- the match ------------------------------------------------------------


def match_invoice(
    *,
    invoice: dict,
    po_status: dict,
    gr_rows: list[dict],
    tolerances: Tolerances,
) -> dict:
    """Compare one extracted invoice line against its PO line and goods-receipt
    sum. Pure. `invoice` carries the extracted VALUES only (no confidences):
    po_number, po_item, quantity, unit_price, currency, vendor_name,
    vendor_invoice_number, material_description.

    Returns a structured record:
      outcome: "clean" | "variance" | "po_line_not_found" | "ambiguous_po_line"
      matched: bool (True only when outcome == "clean")
      variances: [{type, ...numbers, detail}]   (fixed taxonomy)
      computed: {every number rendered from this struct}   (email/UI read these)
      candidates: [...]  (when the PO line couldn't be pinned to exactly one)
    """
    gr_match = compare_po_to_receipts(po_status, gr_rows)
    po_supplier = str(po_status.get("supplier", ""))
    po_supplier_name = str(po_status.get("supplier_name", ""))
    po_currency = str(po_status.get("currency", ""))

    line_res = find_po_line(
        po_status.get("items", []),
        str(invoice.get("po_item", "")),
        str(invoice.get("material_description", "")),
    )
    base = {
        "purchase_order": po_status.get("purchase_order", ""),
        "company_code": po_status.get("company_code", ""),
        "invoice_vendor_name": invoice.get("vendor_name", ""),
        "vendor_invoice_number": invoice.get("vendor_invoice_number", ""),
        "po_supplier": po_supplier,
        "po_supplier_name": po_supplier_name,
    }
    if not line_res["found"]:
        outcome = "ambiguous_po_line" if line_res["ambiguous"] else "po_line_not_found"
        return {
            **base,
            "outcome": outcome,
            "matched": False,
            "variances": [],
            "computed": {},
            "match_method": line_res["method"],
            "candidates": [
                {"item": c.get("item", ""), "material": c.get("material", ""),
                 "description": c.get("description", ""),
                 "ordered": c.get("quantity", ""), "unit": c.get("unit", "")}
                for c in line_res["candidates"]
            ],
        }

    line = line_res["line"]
    item_no = str(line.get("item", "")).strip()
    ordered_qty = _dec(line.get("quantity")) or Decimal("0")
    po_unit_price = _dec(line.get("net_price"))
    unit = str(line.get("unit", ""))

    # Received quantity for this exact line, from the reuse of the 2-way match.
    received_qty = Decimal("0")
    for gm in gr_match.get("items", []):
        if _item_matches_number(str(gm.get("item", "")), item_no):
            received_qty = _dec(gm.get("received")) or Decimal("0")
            break

    gr_reference = _find_gr_reference(gr_rows, item_no)

    inv_qty = _dec(_num(invoice.get("quantity"))) or Decimal("0")
    inv_price = _dec(_num(invoice.get("unit_price")))
    inv_currency = str(invoice.get("currency", "")).strip().upper()
    inv_line_amount = (inv_qty * inv_price) if inv_price is not None else None

    variances: list[dict] = []

    # 1) Currency
    if inv_currency and po_currency and inv_currency != po_currency.upper():
        variances.append({
            "type": CURRENCY_MISMATCH,
            "invoice_currency": inv_currency,
            "po_currency": po_currency,
            "detail": f"Invoice is in {inv_currency}; PO is in {po_currency}.",
        })

    # 2) Vendor (soft: only assertable when the PO exposes a supplier name)
    vendor_check = _vendor_check(invoice.get("vendor_name", ""), po_supplier_name)
    if vendor_check is not None:
        variances.append(vendor_check)

    # 3) Missing goods receipt supersedes a quantity variance - with nothing
    #    received there is no 3-way match to make.
    if received_qty <= 0:
        variances.append({
            "type": MISSING_GR,
            "ordered": str(ordered_qty),
            "received": "0",
            "invoiced": str(inv_qty),
            "unit": unit,
            "detail": "No goods receipt found for this PO line - cannot 3-way "
                      "match. Invoice may precede delivery.",
        })
    else:
        qty_delta = inv_qty - received_qty
        if _breaches(qty_delta, received_qty, tolerances.quantity_pct, tolerances.quantity_abs):
            variances.append({
                "type": QUANTITY_VARIANCE,
                "invoiced": str(inv_qty),
                "received": str(received_qty),
                "ordered": str(ordered_qty),
                "unit": unit,
                "delta": str(qty_delta),
                "detail": f"Invoiced {inv_qty} {unit} but {received_qty} {unit} "
                          f"were received (delta {qty_delta}).",
            })

    # 4) Price (per unit). No PO price -> can't confirm; flag conservatively.
    if inv_price is None:
        variances.append({
            "type": PRICE_VARIANCE,
            "invoice_unit_price": "",
            "po_unit_price": str(po_unit_price) if po_unit_price is not None else "",
            "detail": "Invoice unit price could not be read as a number.",
        })
    elif po_unit_price is None:
        variances.append({
            "type": PRICE_VARIANCE,
            "invoice_unit_price": str(inv_price),
            "po_unit_price": "",
            "detail": "PO carries no net price for this line - unit price cannot "
                      "be verified (needs a purchasing info record / master data).",
        })
    else:
        price_delta = inv_price - po_unit_price
        if _breaches(price_delta, po_unit_price, tolerances.price_pct, tolerances.price_abs):
            variances.append({
                "type": PRICE_VARIANCE,
                "invoice_unit_price": str(inv_price),
                "po_unit_price": str(po_unit_price),
                "currency": po_currency,
                "delta": str(price_delta),
                "detail": f"Invoiced {inv_price} {po_currency}/unit vs PO price "
                          f"{po_unit_price} {po_currency}/unit (delta {price_delta}).",
            })

    matched = not variances
    computed = {
        "po_item": item_no,
        "material": line.get("material", ""),
        "unit": unit,
        "ordered_qty": str(ordered_qty),
        "received_qty": str(received_qty),
        "invoiced_qty": str(inv_qty),
        "po_unit_price": str(po_unit_price) if po_unit_price is not None else "",
        "invoice_unit_price": str(inv_price) if inv_price is not None else "",
        "invoice_line_amount": str(inv_line_amount) if inv_line_amount is not None else "",
        "currency": po_currency or inv_currency,
        "gr_material_document": gr_reference["document"],
        "gr_material_document_year": gr_reference["fiscal_year"],
        "gr_material_document_item": gr_reference["item"],
    }
    return {
        **base,
        "outcome": "clean" if matched else "variance",
        "matched": matched,
        "match_method": line_res["method"],
        "variances": variances,
        "variance_types": [v["type"] for v in variances],
        "computed": computed,
        "candidates": [],
        "gr_reference_ambiguous": gr_reference["multiple"],
        # Raw posting prerequisites read off the PO this match already fetched -
        # facts, not verdicts. propose_post_supplier_invoice judges them through
        # invoice_preflight.run_preflight, so the pre-flight needs no PO read of
        # its own and can never disagree with the line this match resolved.
        "posting_prerequisites": collect_prerequisites(po_status, line, gr_reference),
    }


def _vendor_check(invoice_vendor: str, po_supplier_name: str) -> dict | None:
    """Return a VENDOR_MISMATCH variance only when we can actually assert one:
    the PO must expose a supplier name AND it must share no significant word
    with the invoice's vendor name. When the tenant returns no PO supplier name
    (common - the Business Partner name API is not authorised here), we cannot
    verify the vendor, so we do NOT fabricate a mismatch; the caller notes it
    as unverified instead."""
    inv = re.sub(r"[^a-z0-9 ]", " ", (invoice_vendor or "").lower()).split()
    po = re.sub(r"[^a-z0-9 ]", " ", (po_supplier_name or "").lower()).split()
    if not inv or not po:
        return None
    stop = {"inc", "co", "ltd", "llc", "gmbh", "corp", "company", "the", "and"}
    inv_sig = {w for w in inv if w not in stop}
    po_sig = {w for w in po if w not in stop}
    if inv_sig & po_sig:
        return None
    return {
        "type": VENDOR_MISMATCH,
        "invoice_vendor": invoice_vendor,
        "po_supplier_name": po_supplier_name,
        "detail": f"Invoice vendor '{invoice_vendor}' does not match PO supplier "
                  f"'{po_supplier_name}'.",
    }

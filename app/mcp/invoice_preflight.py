"""TIER 1 - deterministic pre-flight checks for supplier-invoice posting.

Pure functions, no I/O, no LLM - the same trust boundary rule as
invoice_matching.py. Everything here is decided in plain Python from facts the
caller already read off the PO, BEFORE any POST is sent to SAP. If a check
fails, the posting is blocked locally and the human gets a specific reason; SAP
is never called at all.

WHY THIS TIER EXISTS
Two real SAP rejections motivated it, and both are now caught here:

1. *Wrong reference shape.* Every PO item carries a GR-based Invoice
   Verification flag (`InvoiceIsGoodsReceiptBased`). When it is OFF the invoice
   item must reference `PurchaseOrder` + `PurchaseOrderItem` only; when it is ON
   it must ALSO reference the goods-receipt material document
   (`ReferenceDocument` + `-FiscalYear` + `-Item`). The old code always built
   the GR-document reference, so every PO-based item was rejected with "Only
   fill ReferenceDocument/-FiscalYear/-Item if GR-based IV is active" plus
   "Item not selectable". `reference_mode` below is the single place that
   decision is now made.

2. *Missing payment terms.* `PaymentTerms` is required to post and is inherited
   from the vendor master when the PO is created. When it is blank that is an
   upstream master-data gap - so this module BLOCKS and explains, rather than
   defaulting or guessing a value. That mirrors what the entity resolver does
   with an ambiguous code match: surface it, never guess. The only difference is
   timing - resolution time there, pre-flight time here.

REASON CODES ARE A FIXED SET (below). A check may not invent a code, for the
same reason the matcher's variance taxonomy is fixed: the UI, the review queue
and the error-translation layer all read this one vocabulary.

Each blocker carries three parts, and all three are load-bearing:
    code    - stable, machine-readable, for UI/routing
    message - what is wrong, in a human sentence naming the actual document
    remedy  - who fixes it where. A blocker a person cannot act on is a dead
              end, which is the failure mode this whole tier exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---- fixed reason taxonomy ------------------------------------------------
PAYMENT_TERMS_MISSING = "PAYMENT_TERMS_MISSING"
GR_BASED_IV_UNKNOWN = "GR_BASED_IV_UNKNOWN"
GR_REFERENCE_MISSING = "GR_REFERENCE_MISSING"
PO_DELETED = "PO_DELETED"
PO_ITEM_DELETED = "PO_ITEM_DELETED"
PO_RELEASE_INCOMPLETE = "PO_RELEASE_INCOMPLETE"
INVOICE_NOT_EXPECTED = "INVOICE_NOT_EXPECTED"
ITEM_FINALLY_INVOICED = "ITEM_FINALLY_INVOICED"
SERVICE_BASED_IV = "SERVICE_BASED_IV"

BLOCKER_CODES = frozenset({
    PAYMENT_TERMS_MISSING, GR_BASED_IV_UNKNOWN, GR_REFERENCE_MISSING,
    PO_DELETED, PO_ITEM_DELETED, PO_RELEASE_INCOMPLETE,
    INVOICE_NOT_EXPECTED, ITEM_FINALLY_INVOICED, SERVICE_BASED_IV,
})

# The two reference shapes an invoice item may take. `build_supplier_invoice_payload`
# branches on exactly these strings - they are a contract between the modules.
REFERENCE_MODE_PO = "po-based"
REFERENCE_MODE_GR = "gr-based"


@dataclass(frozen=True)
class PreflightResult:
    """Outcome of tier 1. `blocked` is the gate; `reference_mode` is the
    decision the payload builder needs and is only ever set when it was
    positively determined (never defaulted)."""

    blocked: bool
    reference_mode: str                     # "po-based" | "gr-based" | "" when undetermined
    blockers: tuple[dict, ...] = field(default_factory=tuple)
    warnings: tuple[dict, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "blocked": self.blocked,
            "reference_mode": self.reference_mode,
            "blockers": [dict(b) for b in self.blockers],
            "warnings": [dict(w) for w in self.warnings],
            "codes": [b["code"] for b in self.blockers],
        }

    def summary(self) -> str:
        """One human line per blocker - what a person reads first."""
        if not self.blocked:
            return "All pre-flight checks passed."
        return "\n".join(f"- {b['message']} {b['remedy']}" for b in self.blockers)


def collect_prerequisites(po_status: dict, po_line: dict, gr_reference: dict) -> dict:
    """Gather the raw FACTS tier 1 judges, from the already-fetched PO.

    Facts only - no verdicts. Keeping collection separate from judgement means
    the match record can carry these forward to the propose step (which no
    longer needs its own PO read) while `run_preflight` stays a pure function
    over a plain dict that a test can hand-build.
    """
    return {
        "purchase_order": po_status.get("purchase_order", ""),
        "po_item": str(po_line.get("item", "")).strip(),
        "payment_terms": str(po_status.get("payment_terms", "") or "").strip(),
        "po_deletion_code": str(po_status.get("deletion_code", "") or "").strip(),
        "po_release_incomplete": po_status.get("release_incomplete"),
        "po_item_deleted": bool(po_line.get("deleted")),
        "gr_based_iv": po_line.get("gr_based_iv"),
        "invoice_expected": po_line.get("invoice_expected"),
        "finally_invoiced": po_line.get("finally_invoiced"),
        "service_based_iv": po_line.get("service_based_iv"),
        "gr_reference": {
            "document": gr_reference.get("document", ""),
            "fiscal_year": gr_reference.get("fiscal_year", ""),
            "item": gr_reference.get("item", ""),
        },
    }


def _blocker(code: str, message: str, remedy: str) -> dict:
    return {"code": code, "message": message, "remedy": remedy}


def run_preflight(prerequisites: dict) -> PreflightResult:
    """Judge the facts from `collect_prerequisites`. Pure.

    Returns a PreflightResult whose `reference_mode` is set ONLY when the
    GR-based IV flag was positively read. An unknown flag is a blocker, not a
    default: the whole point of tier 1 is that the reference shape is never
    guessed.
    """
    po = prerequisites.get("purchase_order", "") or "this purchase order"
    item = prerequisites.get("po_item", "") or "?"
    blockers: list[dict] = []
    warnings: list[dict] = []

    # ---- document is postable at all ------------------------------------
    if prerequisites.get("po_deletion_code"):
        blockers.append(_blocker(
            PO_DELETED,
            f"Purchase order {po} is flagged for deletion in SAP "
            f"(deletion code '{prerequisites['po_deletion_code']}').",
            "An invoice cannot be posted against a deleted PO - have Purchasing "
            "reinstate the order, or confirm this invoice belongs to a different PO.",
        ))
    if prerequisites.get("po_item_deleted"):
        blockers.append(_blocker(
            PO_ITEM_DELETED,
            f"Item {item} of purchase order {po} is flagged for deletion.",
            "Have Purchasing reinstate the line, or re-check which PO line this "
            "invoice is actually for.",
        ))
    if prerequisites.get("po_release_incomplete") is True:
        blockers.append(_blocker(
            PO_RELEASE_INCOMPLETE,
            f"Purchase order {po} has not completed its release (approval) strategy.",
            "The PO must be fully released before an invoice can be posted against "
            "it - chase the outstanding approver in Purchasing.",
        ))

    # ---- the line accepts an invoice at all -----------------------------
    if prerequisites.get("invoice_expected") is False:
        blockers.append(_blocker(
            INVOICE_NOT_EXPECTED,
            f"Item {item} of purchase order {po} is not flagged for invoice receipt "
            "(SAP's 'Invoice Receipt' indicator is off on the PO line).",
            "SAP will not let any invoice select this item. Either Purchasing sets "
            "the invoice-receipt indicator on the PO line, or this invoice is for a "
            "different line - confirm before posting.",
        ))
    if prerequisites.get("finally_invoiced") is True:
        blockers.append(_blocker(
            ITEM_FINALLY_INVOICED,
            f"Item {item} of purchase order {po} is already marked 'finally invoiced'.",
            "SAP considers this line closed for invoicing. Confirm this is not a "
            "duplicate; if a further invoice is genuinely due, Purchasing must clear "
            "the final-invoice indicator on the PO line.",
        ))
    if prerequisites.get("service_based_iv") is True:
        blockers.append(_blocker(
            SERVICE_BASED_IV,
            f"Item {item} of purchase order {po} uses service-entry-based invoice "
            "verification.",
            "That line must be invoiced against an accepted service entry sheet, a "
            "reference shape this pipeline does not build. Post it in SAP directly "
            "(MIRO) instead.",
        ))

    # ---- payment terms (bug 2) ------------------------------------------
    # Deliberately NOT defaulted. Payment terms drive the due date and any cash
    # discount; inventing one would silently create a financially wrong document
    # that posts cleanly - far worse than a blocked invoice with a clear reason.
    if not prerequisites.get("payment_terms"):
        blockers.append(_blocker(
            PAYMENT_TERMS_MISSING,
            f"Purchase order {po} carries no payment terms (SAP field PaymentTerms "
            "is empty), and SAP requires them to post a supplier invoice.",
            "This is a master-data gap, not something to guess: have the payment "
            "terms added to the vendor master and/or corrected on the PO header, "
            "then re-run this invoice. No value is defaulted here on purpose - the "
            "terms decide the due date and cash discount.",
        ))

    # ---- reference shape (bug 1) ----------------------------------------
    gr_based = prerequisites.get("gr_based_iv")
    gr_ref = prerequisites.get("gr_reference") or {}
    reference_mode = ""
    if gr_based is None:
        blockers.append(_blocker(
            GR_BASED_IV_UNKNOWN,
            f"Could not read the GR-based invoice-verification flag for item {item} "
            f"of purchase order {po} (SAP field InvoiceIsGoodsReceiptBased was not "
            "returned by this tenant).",
            "Without it there is no safe way to choose between a PO reference and a "
            "goods-receipt reference on the invoice line, and guessing wrong is "
            "rejected by SAP. Check that the PO service release exposes this field.",
        ))
    elif gr_based:
        reference_mode = REFERENCE_MODE_GR
        if not (gr_ref.get("document") and gr_ref.get("fiscal_year") and gr_ref.get("item")):
            blockers.append(_blocker(
                GR_REFERENCE_MISSING,
                f"Item {item} of purchase order {po} uses GR-based invoice "
                "verification, so the invoice must reference the goods receipt - but "
                "no usable goods-receipt document was found for this line.",
                "Post the goods receipt first, then re-run this invoice. (A receipt "
                "that was reversed or cancelled does not count as a reference.)",
            ))
    else:
        reference_mode = REFERENCE_MODE_PO
        # Not an error: a GR may well exist on a PO-based line. It simply must
        # not go on the invoice item, which is the bug this branch fixes.
        if gr_ref.get("document"):
            warnings.append({
                "code": "GR_REFERENCE_IGNORED",
                "message": (
                    f"Item {item} uses PO-based invoice verification, so goods "
                    f"receipt {gr_ref.get('document')}/{gr_ref.get('fiscal_year')} is "
                    "NOT referenced on the invoice line (SAP rejects the reference "
                    "on a PO-based item). The receipt is still used for the 3-way "
                    "quantity match."
                ),
            })

    return PreflightResult(
        blocked=bool(blockers),
        reference_mode=reference_mode,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
    )


def blocked_result(preflight: PreflightResult, *, purchase_order: str = "") -> dict:
    """The structured 'blocked' object returned to the caller instead of a
    proposal. Shaped to be self-explanatory in the HTTP response, the trace
    panel and the chat UI without any of them re-deriving anything:

        ok            False - this is not a proposal and nothing was posted
        outcome       "blocked" - distinct from "variance"/"proposal_failed"
        sap_called    False - the promise tier 1 makes: SAP was never contacted
        blockers      [{code, message, remedy}] - the fixed taxonomy above
        error         a single rendered sentence for callers that show one line
    """
    return {
        "ok": False,
        "outcome": "blocked",
        "blocked": True,
        "sap_called": False,
        "purchase_order": purchase_order,
        "blockers": [dict(b) for b in preflight.blockers],
        "codes": [b["code"] for b in preflight.blockers],
        "warnings": [dict(w) for w in preflight.warnings],
        "error": (
            "This invoice cannot be posted yet - "
            f"{len(preflight.blockers)} pre-flight check(s) failed:\n"
            + preflight.summary()
        ),
    }

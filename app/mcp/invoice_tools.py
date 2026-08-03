"""Supplier-invoice agent: plain-Python tools + the pipeline that sequences them.

Every tool here is a plain function with a clean, typed signature - no FastAPI
request/response objects, no framework state, inside the logic. That is the
constraint that keeps a later AgentCore-Gateway migration a small, optional
step (wrap these functions) rather than a rewrite; keep it that way if you edit
this file. `app/main.py` only translates HTTP <-> these calls.

The user flow is TWO steps, because matching and posting are two different
questions and only the first one has been asked when a document is uploaded:

1. MATCH - `run_invoice_pipeline` reads the document, dedups it, resolves the
   PO line, sums the goods receipts and runs the 3-way match. It ends there,
   at `outcome: "matched"`, with the match parked in the MatchStore. No posting
   check has run, so nothing about master data can bury a clean result.
2. POST - `propose_posting_for_match(match_id)` runs only when a human asks to
   post. That is where the tier-1 pre-flight lives, so its blockers appear at
   the moment they are load-bearing rather than as noise on a match.

The propose/commit gate inside step 2 matches the PO layer's rule exactly:
* `propose_post_supplier_invoice` builds the full payload and returns it plus a
  human-readable summary and a single-use proposal_id. It writes nothing.
* `commit_post_supplier_invoice` is the ONLY writer and takes ONLY a
  proposal_id. It is never placed in any LLM tool list; it is called from
  application code after an explicit human approval - even when the match was
  clean. A clean match lowers risk; it does not remove the approval step.

Both steps emit a structured trace event per step for the live UI panel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

from app.config import settings
from app.mcp.errors import SAPRequestError
from app.mcp.gr import fetch_goods_receipt_items
from app.mcp.invoice_email import SESMailer, send_variance_email
from app.mcp.invoice_error_translation import translate_posting_error
from app.mcp.invoice_extraction import ExtractionError, extract_invoice
from app.mcp.invoice_ledger import EmailDedupLedger, SupplierInvoiceLedger
from app.mcp.invoice_matches import MatchStore
from app.mcp.invoice_matching import Tolerances, match_invoice
from app.mcp.invoice_posting import (
    ProposedInvoice,
    ProposedInvoiceItem,
    apply_field_edits,
    build_proposal_fields,
    build_supplier_invoice_payload,
    parse_supplier_invoice_response,
    payload_hash,
    render_invoice_preview,
)
from app.mcp.invoice_preflight import blocked_result, run_preflight
from app.mcp.invoice_proposals import InvoiceProposalStore
from app.mcp.invoice_read import fetch_invoices_by_reference
from app.mcp.invoice_review import ReviewQueue
from app.mcp.invoice_trace import trace_store

log = logging.getLogger("sap-mcp.invoice")


# ---- runtime-overridable tolerances (no restart) --------------------------


class _ToleranceStore:
    """Holds the live tolerances, seeded from config. The runtime endpoint
    (POST /api/invoice/tolerances) updates these in place - same spirit as the
    per-request knowledge-base toggle: no restart to change matching behaviour."""

    def __init__(self) -> None:
        self._t = Tolerances.from_settings(settings)

    def get(self) -> Tolerances:
        return self._t

    def update(
        self,
        price_pct: float | None = None,
        price_abs: float | None = None,
        quantity_pct: float | None = None,
        quantity_abs: float | None = None,
    ) -> Tolerances:
        current = self._t
        self._t = Tolerances(
            price_pct=price_pct if price_pct is not None else current.price_pct,
            price_abs=price_abs if price_abs is not None else current.price_abs,
            quantity_pct=quantity_pct if quantity_pct is not None else current.quantity_pct,
            quantity_abs=quantity_abs if quantity_abs is not None else current.quantity_abs,
        )
        return self._t


_tolerances = _ToleranceStore()


def get_tolerances() -> dict:
    return {"ok": True, "tolerances": _tolerances.get().as_dict()}


def update_tolerances(**kwargs) -> dict:
    """Override any subset of the four tolerance floors at runtime. Ignores
    unknown keys and non-numbers so a bad request can't corrupt the store."""
    clean = {}
    for key in ("price_pct", "price_abs", "quantity_pct", "quantity_abs"):
        if key in kwargs and kwargs[key] is not None:
            try:
                clean[key] = float(kwargs[key])
            except (TypeError, ValueError):
                return {"ok": False, "error": f"'{key}' must be a number."}
    updated = _tolerances.update(**clean)
    log.info("Tolerances updated at runtime: %s", updated.as_dict())
    return {"ok": True, "tolerances": updated.as_dict()}


# ---- dependencies ---------------------------------------------------------


@dataclass
class _InvoiceDeps:
    provider: object                 # BedrockProvider: vision (extract) + generate (email)
    proposals: InvoiceProposalStore
    matches: MatchStore
    ledger: SupplierInvoiceLedger
    email_ledger: EmailDedupLedger
    review_queue: ReviewQueue


@lru_cache(maxsize=1)
def _deps() -> _InvoiceDeps:
    from app.generation.bedrock import BedrockProvider

    provider = BedrockProvider(
        aws_region=settings.aws_region,
        model_id=settings.bedrock_model_id,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )
    return _InvoiceDeps(
        provider=provider,
        proposals=InvoiceProposalStore(settings.sap_preview_ttl_seconds),
        matches=MatchStore(settings.sap_preview_ttl_seconds),
        ledger=SupplierInvoiceLedger(settings.sap_supplier_invoice_ledger_path),
        email_ledger=EmailDedupLedger(settings.sap_invoice_email_ledger_path),
        review_queue=ReviewQueue(settings.invoice_review_queue_path),
    )


def _sap():
    """The shared SAP client + PO read, reused from the PO layer so both layers
    share one authenticated session. Returns the server._Deps or raises
    SAPRequestError (credentials/config)."""
    from app.mcp.server import _deps as sap_deps

    return sap_deps()


def _recipients() -> list[str]:
    return [r.strip() for r in settings.invoice_email_recipients.split(",") if r.strip()]


def _new_mailer() -> SESMailer:
    """A fresh mailer per pipeline run, so the send cap is genuinely per-run.

    The AWS keys are passed through for the same reason BedrockProvider gets
    them above: `.env` values never reach os.environ, so a client left to
    boto3's default chain finds nothing.
    """
    return SESMailer(
        region=settings.ses_region,
        sender=settings.ses_sender,
        cap=settings.invoice_email_max_sends_per_run,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )


# ---- individual tools (clean signatures, no framework objects) ------------


def extract_invoice_fields(file_bytes: bytes, content_type: str) -> dict:
    """Read a supplier invoice image/PDF into fields with per-field confidence.

    Returns {ok, extracted: {field: {value, confidence}}, low_confidence:
    [every critical field below threshold], blocking_low_confidence: [the subset
    with no recovery route], needs_review: bool, review_reason, po_item_readable,
    description_fallback_available}.

    An unreadable PO *item* number alone does NOT force review: it has a designed
    second route (the flagged material-description fallback). It only blocks when
    there is no usable description to fall back on.
    """
    try:
        extracted = extract_invoice(_deps().provider, file_bytes, content_type)
    except ExtractionError as exc:
        return {"ok": False, "error": str(exc)}
    threshold = settings.invoice_confidence_threshold
    low = extracted.low_confidence_fields(threshold)
    blocking = extracted.blocking_low_confidence_fields(threshold)
    po_item_readable = extracted.po_item_is_readable(threshold)
    fallback_available = extracted.has_description_fallback(threshold)

    reason = ""
    if blocking:
        reason = (
            "Low confidence on field(s) with no recovery route: "
            + ", ".join(blocking)
        )
    elif not po_item_readable and not fallback_available:
        reason = (
            "PO item number is unreadable and the material description was not "
            "read confidently enough to fall back on."
        )
    return {
        "ok": True,
        "extracted": extracted.as_dict(),
        "values": {k: v["value"] for k, v in extracted.as_dict().items()},
        "low_confidence": low,
        "blocking_low_confidence": blocking,
        "needs_review": bool(reason),
        "review_reason": reason,
        "po_item_readable": po_item_readable,
        "description_fallback_available": fallback_available,
    }


def check_duplicate_invoice(vendor: str, vendor_invoice_number: str) -> dict:
    """Ledger-based duplicate check (vendor + vendor invoice number), run before
    the PO lookup. A hit STOPS the pipeline and surfaces the existing document.
    Returns {ok, is_duplicate, existing?}."""
    if not vendor_invoice_number.strip():
        return {"ok": True, "is_duplicate": False,
                "note": "No vendor invoice number extracted - cannot dedup on reference."}
    hit = _deps().ledger.find_duplicate(vendor, vendor_invoice_number)
    if hit:
        return {"ok": True, "is_duplicate": True, "source": "ledger", "existing": hit}
    return {"ok": True, "is_duplicate": False}


# SupplierInvoiceIDByInvcgParty is MaxLength=16 in the tenant's $metadata; a
# longer reference 400s the filter (and would be rejected at posting too).
_VENDOR_REF_MAXLEN = 16


def check_duplicate_by_supplier_code(invoicing_party: str, vendor_invoice_number: str) -> dict:
    """Second ledger duplicate check, keyed on the SAP supplier code rather than
    the vendor text. Runs once the PO lookup has resolved the code - it catches a
    resubmission of a bill we already posted whose vendor NAME the extractor read
    differently the second time. Offline, so it still works when SAP is not
    reachable. Returns {ok, is_duplicate, existing?}."""
    hit = _deps().ledger.find_duplicate_by_party(invoicing_party, vendor_invoice_number)
    if hit:
        return {"ok": True, "is_duplicate": True, "source": "ledger", "existing": hit}
    return {"ok": True, "is_duplicate": False}


def check_duplicate_in_sap(invoicing_party: str, vendor_invoice_number: str) -> dict:
    """Authoritative live cross-check once the supplier code is known: does SAP
    already carry an invoice with this reference from this supplier? Catches
    duplicates posted outside this tool. Read-only."""
    if not (invoicing_party and vendor_invoice_number):
        return {"ok": True, "is_duplicate": False}
    if len(vendor_invoice_number) > _VENDOR_REF_MAXLEN:
        # Don't send a query SAP will reject on the field facet; flag instead.
        return {"ok": True, "is_duplicate": False,
                "note": f"Vendor invoice number exceeds SAP's {_VENDOR_REF_MAXLEN}-char "
                        "limit for SupplierInvoiceIDByInvcgParty - live cross-check "
                        "skipped; it must be shortened before posting."}
    try:
        rows = fetch_invoices_by_reference(
            _sap().client, settings.sap_supplier_invoice_service,
            invoicing_party, vendor_invoice_number,
        )
    except SAPRequestError as exc:
        # A failed cross-check must not fabricate or suppress a duplicate; report it.
        return {"ok": False, "error": exc.user_message, "is_duplicate": False}
    if rows:
        existing = rows[0]
        return {"ok": True, "is_duplicate": True, "source": "sap", "existing": {
            "supplier_invoice": existing.get("SupplierInvoice", ""),
            "fiscal_year": existing.get("FiscalYear", ""),
            "gross_amount": str(existing.get("InvoiceGrossAmount", "")),
        }}
    return {"ok": True, "is_duplicate": False}


def match_supplier_invoice(
    *,
    po_number: str,
    po_item: str,
    quantity: str,
    unit_price: str,
    currency: str,
    vendor_name: str,
    vendor_invoice_number: str,
    material_description: str = "",
) -> dict:
    """Fetch the PO line + goods-receipt sum and run the 3-way match. Read-only.

    Returns {ok, match: <matcher record>} or {ok: False, error}. The match
    record's `matched`/`outcome`/`variances` drive the propose-or-email fork.
    """
    number = po_number.strip().replace("'", "")
    if not number:
        return {"ok": False, "error": "po_number is required to match an invoice."}
    from app.mcp.server import _fetch_po_status

    try:
        sap = _sap()
        po_status = _fetch_po_status(sap, number)
        if not po_status.get("ok"):
            return po_status
        gr_rows = fetch_goods_receipt_items(sap.client, settings.sap_gr_service, number)
    except SAPRequestError as exc:
        return {"ok": False, "error": exc.user_message}

    record = match_invoice(
        invoice={
            "po_number": number, "po_item": po_item, "quantity": quantity,
            "unit_price": unit_price, "currency": currency,
            "vendor_name": vendor_name, "vendor_invoice_number": vendor_invoice_number,
            "material_description": material_description,
        },
        po_status=po_status,
        gr_rows=gr_rows,
        tolerances=_tolerances.get(),
    )
    return {"ok": True, "match": record}


def propose_post_supplier_invoice(
    *,
    match_record: dict,
    company_code: str = "",
    tax_code: str = "",
    document_date: str = "",
    posting_date: str = "",
    tax_determination_date: str = "",
    confidences: dict | None = None,
) -> dict:
    """Build the full A_SupplierInvoice deep-insert payload from a match record
    and return it plus a human-readable summary, an editable SAP-labelled
    field list (`fields` - see build_proposal_fields), and a single-use
    proposal_id. WRITES NOTHING. A human approves (optionally editing fields
    via update_proposal_fields first), then application code calls
    commit_post_supplier_invoice(proposal_id).

    tax_determination_date defaults to posting_date: SAP's time-dependent-tax
    check requires it on the header, and there is no separate OCR read for it
    (it is a posting concept, not something printed on the invoice) - the
    posting date is the correct default, editable like any other field before
    approval.

    `confidences` (optional) is the extractor's {field_name: 0..1} map -
    passed through so OCR-derived proposal fields (vendor invoice number,
    currency, gross/line amounts) carry a real confidence score in `fields`
    instead of a fabricated one.
    """
    computed = match_record.get("computed", {})
    if match_record.get("outcome") in ("po_line_not_found", "ambiguous_po_line"):
        return {"ok": False, "error": "Cannot propose a posting: the PO line is not "
                "uniquely resolved. Resolve the line first.",
                "candidates": match_record.get("candidates", [])}
    amount = computed.get("invoice_line_amount", "")
    if not amount:
        return {"ok": False, "error": "Cannot propose a posting: no invoice line amount "
                "(unit price or quantity was unreadable)."}

    # ---- TIER 1: deterministic pre-flight, before anything is built -------
    # Judged from facts the match already read off the PO (posting_prerequisites).
    # A match record without them - an older record, or one built by hand - has
    # no readable GR-based-IV flag and no payment terms, so run_preflight blocks
    # it on those codes. That is the safe direction by construction: there is no
    # input to this call that can silently produce an unchecked posting.
    preflight = run_preflight(match_record.get("posting_prerequisites") or {})
    if preflight.blocked:
        log.info("Invoice posting blocked by pre-flight for PO %s: %s",
                 match_record.get("purchase_order", ""),
                 ", ".join(b["code"] for b in preflight.blockers))
        return blocked_result(preflight,
                              purchase_order=match_record.get("purchase_order", ""))

    invoicing_party = match_record.get("po_supplier", "")
    vendor_invoice_number = match_record.get("vendor_invoice_number", "")
    currency = computed.get("currency", "") or settings.sap_default_currency
    today = date.today().isoformat()
    item = ProposedInvoiceItem(
        supplier_invoice_item="1",
        purchase_order=match_record.get("purchase_order", ""),
        purchase_order_item=computed.get("po_item", ""),
        quantity=computed.get("invoiced_qty", ""),
        unit=computed.get("unit", ""),
        amount=amount,
        tax_code=tax_code or settings.invoice_default_tax_code,
        gr_document=computed.get("gr_material_document", ""),
        gr_fiscal_year=computed.get("gr_material_document_year", ""),
        gr_item=computed.get("gr_material_document_item", ""),
        # The reference shape, decided by tier 1 from the PO item's GR-based-IV
        # flag - never defaulted here. The GR fields above stay populated either
        # way (they are what the 3-way match found); build_supplier_invoice_payload
        # is what decides whether they are actually sent to SAP.
        reference_mode=preflight.reference_mode,
    )
    resolved_posting_date = posting_date or today
    proposed = ProposedInvoice(
        company_code=company_code or match_record.get("company_code", "") or settings.sap_default_company_code,
        document_date=document_date or today,
        posting_date=resolved_posting_date,
        tax_determination_date=tax_determination_date or resolved_posting_date,
        invoicing_party=invoicing_party,
        vendor_invoice_number=vendor_invoice_number,
        currency=currency,
        gross_amount=amount,   # single net line; gross == net under a 0% tax code
        items=(item,),
    )

    warnings: list[str] = []
    if not match_record.get("matched"):
        warnings.append(
            "This invoice did NOT fully match (" +
            ", ".join(match_record.get("variance_types", [])) +
            "). Posting it accepts those variances - confirm with a human."
        )
    if match_record.get("match_method") == "material-description-fallback":
        warnings.append("PO line was resolved by material-description fallback, not a "
                        "stated item number - double-check it is the right line.")
    # Only meaningful when the GR document is actually referenced on the posting.
    # On a PO-based item no goods receipt is sent at all, so "which receipt was
    # used as the reference" would be a warning about something that did not
    # happen - the exact half-updated-feature drift this codebase keeps hitting.
    if match_record.get("gr_reference_ambiguous") and item.reference_mode == "gr-based":
        warnings.append(
            "This PO line has more than one goods receipt - the most recent one "
            f"(Material Document {item.gr_document}/{item.gr_fiscal_year}) was used as "
            "the posting reference; double-check that's the delivery this invoice is for.")
    # Non-blocking observations from tier 1 (e.g. a goods receipt exists but is
    # deliberately not referenced because the line is PO-based).
    warnings.extend(w["message"] for w in preflight.warnings)
    warnings.append(
        f"Gross is set equal to the net line amount assuming a 0%-rated tax code "
        f"({item.tax_code}); a non-zero rate needs the gross grossed-up or SAP "
        "will reject the balance.")

    if not invoicing_party:
        return {"ok": False, "error": "Cannot propose a posting: the PO carries no "
                "supplier code to use as InvoicingParty."}

    payload = build_supplier_invoice_payload(proposed)
    digest = payload_hash(payload)
    match_summary = "clean 3-way match" if match_record.get("matched") else \
        "variances present: " + ", ".join(match_record.get("variance_types", []))
    summary = render_invoice_preview(proposed, match_summary, warnings)

    # The vendor TEXT off the document is the duplicate identity the ledger is
    # keyed on (invoicing_party is stored alongside it as the second key). Using
    # the supplier code here instead would record under one identity and look up
    # under another, so no duplicate this pipeline posted would ever be found.
    vendor_text = match_record.get("invoice_vendor_name", "") or match_record.get("po_supplier", "")

    twin = (_deps().ledger.find_duplicate(vendor_text, vendor_invoice_number)
            or _deps().ledger.find_duplicate_by_party(invoicing_party, vendor_invoice_number))
    if twin:
        warnings.append(
            f"An invoice with this vendor reference was already posted "
            f"(SupplierInvoice {twin.get('supplier_invoice')}) at {twin.get('created_at')}.")
        summary = render_invoice_preview(proposed, match_summary, warnings)

    clean_confidences = {k: v for k, v in (confidences or {}).items() if v is not None}
    entry = _deps().proposals.put(
        payload=payload,
        payload_hash=digest,
        summary=summary,
        vendor=vendor_text,
        vendor_invoice_number=vendor_invoice_number,
        invoicing_party=invoicing_party,
        purchase_order=match_record.get("purchase_order", ""),
        proposed=proposed,
        confidences=clean_confidences,
        match_summary=match_summary,
        warnings=warnings,
    )
    fields = build_proposal_fields(proposed, clean_confidences)
    log.info("Invoice proposal %s minted (payload %s...)", entry.proposal_id, digest[:12])
    return {
        "ok": True,
        "proposal_id": entry.proposal_id,
        "summary": summary,
        "payload": payload,
        "fields": fields,
        "matched": bool(match_record.get("matched")),
        "warnings": warnings,
        "instructions": (
            "Show `summary` (or render `fields`) to a human and get explicit approval - "
            "they may edit any non-locked field first via update_proposal_fields. Only "
            f"then call commit_post_supplier_invoice(proposal_id='{entry.proposal_id}')."
        ),
    }


def update_proposal_fields(proposal_id: str, edits: dict) -> dict:
    """Apply human edits to a not-yet-approved proposal, in place under the
    SAME proposal_id (commit still takes only the id - see
    commit_post_supplier_invoice). PurchaseOrder/PurchaseOrderItem are locked:
    editing either would silently detach the posting from the 3-way match
    that approved it, so those keys come back in `rejected_fields` untouched.
    Returns {ok, proposal_id, summary, payload, fields, rejected_fields} or
    {ok: False, error} if the proposal is unknown, already posted, or expired.
    """
    deps = _deps()
    entry, reason = deps.proposals.claim(proposal_id)
    if entry is None:
        return {"ok": False, "error": reason}
    if entry.proposed is None:
        return {"ok": False, "error": "This proposal predates edit support - "
                "re-run the match and re-propose to get an editable one."}

    updated, rejected = apply_field_edits(entry.proposed, edits)
    payload = build_supplier_invoice_payload(updated)
    digest = payload_hash(payload)
    summary = render_invoice_preview(updated, entry.match_summary, entry.warnings)
    deps.proposals.update(proposal_id, payload=payload, payload_hash=digest,
                           summary=summary, proposed=updated)
    fields = build_proposal_fields(updated, entry.confidences)
    return {
        "ok": True,
        "proposal_id": proposal_id,
        "summary": summary,
        "payload": payload,
        "fields": fields,
        "warnings": entry.warnings,
        "rejected_fields": rejected,
    }


def commit_post_supplier_invoice(proposal_id: str) -> dict:
    """Post a previously proposed, human-approved supplier invoice to SAP. THE
    ONLY tool that writes. Takes only a proposal_id. Safe to retry: the ledger
    guarantees at most one document per approved proposal.

    NOT exposed to any LLM tool list - call this from application code after an
    explicit human approval.
    """
    deps = _deps()
    entry, reason = deps.proposals.claim(proposal_id)
    if entry is None:
        already = deps.ledger.find_by_proposal(proposal_id)
        if already:
            return {"ok": True, "duplicate_suppressed": True,
                    "supplier_invoice": already["supplier_invoice"],
                    "fiscal_year": already["fiscal_year"],
                    "message": f"Proposal already posted - SupplierInvoice "
                               f"{already['supplier_invoice']}/{already['fiscal_year']} exists."}
        return {"ok": False, "error": reason}

    key = deps.ledger.key(entry.payload_hash, proposal_id)
    existing = deps.ledger.find(key)
    if existing:
        deps.proposals.mark_consumed(proposal_id)
        return {"ok": True, "duplicate_suppressed": True,
                "supplier_invoice": existing["supplier_invoice"],
                "fiscal_year": existing["fiscal_year"],
                "message": f"Proposal already posted - SupplierInvoice "
                           f"{existing['supplier_invoice']}/{existing['fiscal_year']} exists."}

    try:
        service = settings.sap_supplier_invoice_service
        data = _sap().client.post(
            service, f"{service}/A_SupplierInvoice", entry.payload,
            context="posting the supplier invoice",
        )
    except SAPRequestError as exc:
        # TIER 2: SAP rejected something tier 1 did not catch. Translate it for
        # the human. This is explanation ONLY - the retry_hint below is fixed
        # application text written here, never anything the model produced, and
        # nothing is retried or re-posted as a result of the translation. The
        # proposal is left unconsumed exactly as before, so the human still
        # decides what happens next.
        translation = translate_posting_error(
            exc.user_message, raw_body=exc.raw or "", provider=_deps().provider,
        )
        log.info("Supplier invoice POST rejected (proposal %s); explanation source: %s",
                 proposal_id, translation.get("source"))
        return {"ok": False,
                "error": exc.user_message,
                "explanation": translation.get("explanation", ""),
                "explanation_source": translation.get("source", ""),
                "sap_called": True,
                "retry_hint": "The proposal is still valid. Retry with the same "
                "proposal_id if this looks transient; if SAP rejected a field, "
                "re-run the match and re-propose."}

    parsed = parse_supplier_invoice_response(data)
    doc = parsed["supplier_invoice"]
    if not doc:
        log.warning("Supplier invoice POST returned 2xx but no document number: %s", str(data)[:400])
        return {"ok": False, "error": "SAP accepted the request but returned no "
                "SupplierInvoice number. Verify in SAP before retrying."}
    record = deps.ledger.record(
        payload_hash=entry.payload_hash, proposal_id=proposal_id,
        vendor=entry.vendor, vendor_invoice_number=entry.vendor_invoice_number,
        supplier_invoice=doc, fiscal_year=parsed["fiscal_year"],
        invoicing_party=entry.invoicing_party, purchase_order=entry.purchase_order,
    )
    deps.proposals.mark_consumed(proposal_id)
    log.info("Posted SupplierInvoice %s/%s (proposal %s)", doc, parsed["fiscal_year"], proposal_id)
    return {
        "ok": True,
        "supplier_invoice": doc,
        "fiscal_year": parsed["fiscal_year"],
        "status_code": parsed.get("status_code", ""),
        "ledger_record": record,
        "message": f"Supplier invoice {doc}/{parsed['fiscal_year']} posted in SAP.",
    }


def list_review_queue() -> dict:
    """Open items a human still needs to handle (low-confidence extractions)."""
    return {"ok": True, "items": _deps().review_queue.list_open()}


def send_variance_notification(match_record: dict, mailer: SESMailer | None = None) -> dict:
    """Guarded SES send of a variance email for a match record (dedup + numeral
    guard + per-run cap live in invoice_email). Returns the structured result."""
    return send_variance_email(
        provider=_deps().provider,
        match_record=match_record,
        recipients=_recipients(),
        language=settings.invoice_email_language,
        mailer=mailer or _new_mailer(),
        dedup_ledger=_deps().email_ledger,
    )


# ---- the full pipeline (trace-emitting orchestrator) ----------------------


def _resume_after_match(
    run_id: str, record: dict, values: dict, extracted: dict,
    used_fallback: bool, auto_send_email: bool,
) -> dict:
    """Shared tail from match_supplier_invoice's result onward: candidate
    parking, the duplicate cross-checks, and the clean-match/variance fork.

    Used by both run_invoice_pipeline (a fresh OCR read) and
    retry_match_with_corrected_po (a human-corrected PO number re-entering
    the same flow after a match failure) so the two paths share one
    implementation and can't drift apart.
    """
    result: dict = {"ok": True, "run_id": run_id}

    def emit(step, label, status="ok", tool="", data=None):
        trace_store.emit(run_id, step, label, status, tool, data)

    # PO line could not be pinned to exactly one candidate -> surface the
    # candidates and PARK it for a human. Returning them in the HTTP response
    # alone would lose the case the moment the response is gone; the review
    # queue is what makes "don't guess" an actual handoff rather than a dead end.
    if record["outcome"] in ("po_line_not_found", "ambiguous_po_line"):
        review = _deps().review_queue.enqueue({
            "reason": record["outcome"],
            "detail": ("Several PO lines could match this invoice."
                       if record["outcome"] == "ambiguous_po_line"
                       else "No PO line on this order matches the invoice."),
            "purchase_order": record.get("purchase_order", ""),
            "match_method": record.get("match_method", ""),
            "used_description_fallback": used_fallback,
            "candidates": record.get("candidates", []),
            "extracted": extracted,
            "values": values,
        })
        emit("review", f"Could not resolve the PO line ({record['outcome']}) - surfacing "
             "candidates and routing to human review, not guessing.", "warn",
             "list_review_queue", data={"candidates": record.get("candidates", []),
                                        "review_id": review.get("id")})
        trace_store.finish_run(run_id)
        return {**result, "outcome": record["outcome"], "match": record,
                "review": review, "extracted": extracted}

    # Duplicate checks that need the supplier code: the ledger keyed on that
    # code (offline, catches an OCR'd name drift the text key missed), then the
    # authoritative live cross-check against SAP itself.
    supplier_code = record.get("po_supplier", "")
    reference = values.get("vendor_invoice_number", "")
    party_dup = check_duplicate_by_supplier_code(supplier_code, reference)
    if party_dup.get("is_duplicate"):
        emit("duplicate", "Already posted for this supplier + reference (vendor name "
             "read differently this time) - stopping.", "warn",
             "check_duplicate_by_supplier_code",
             data={"existing": party_dup.get("existing", {})})
        trace_store.finish_run(run_id)
        return {**result, "outcome": "duplicate", "duplicate": party_dup, "match": record,
                "extracted": extracted}

    live_dup = check_duplicate_in_sap(supplier_code, reference)
    if live_dup.get("is_duplicate"):
        emit("duplicate", "SAP already has an invoice with this vendor reference - stopping.",
             "warn", "check_duplicate_in_sap", data={"existing": live_dup.get("existing", {})})
        trace_store.finish_run(run_id)
        return {**result, "outcome": "duplicate", "duplicate": live_dup, "match": record,
                "extracted": extracted}

    if record["matched"]:
        emit("match", "Clean 3-way match.", "ok",
             data={"computed": record.get("computed", {}),
                   "match_method": record.get("match_method", "")})
        # STEP 1 ENDS HERE. Matching answers "do the invoice, the PO and the
        # goods receipt agree?"; posting is a separate question the user has
        # not asked yet. Running the posting pre-flight now would answer it
        # anyway and report a wall of blockers about master data - on an
        # invoice that matched perfectly - to someone who only wanted the
        # match. So the match is parked and the pre-flight waits for an
        # explicit request to post (propose_posting_for_match).
        entry = _deps().matches.put(run_id=run_id, record=record, values=values,
                                    extracted=extracted)
        emit("ready", "Nothing has been posted and no posting checks have run yet - "
             "matching and posting are separate steps. Continue to posting when you "
             "want this invoice in SAP.", "ok")
        trace_store.finish_run(run_id)
        return {**result, "outcome": "matched", "match": record,
                "match_id": entry.match_id, "extracted": extracted}

    # Variance -> structured record -> variance email
    emit("match", "Variance found: " + ", ".join(record.get("variance_types", [])), "warn",
         data={"variances": record.get("variances", []), "computed": record.get("computed", {}),
               "match_method": record.get("match_method", "")})
    email_result = {"ok": True, "sent": False, "reason": "email not attempted"}
    if auto_send_email:
        emit("email", "Drafting and sending a variance notification...", "running",
             "send_variance_notification")
        email_result = send_variance_notification(record)
        status = "ok" if email_result.get("sent") else ("warn" if email_result.get("ok") else "error")
        emit("email", email_result.get("reason", "Variance email processed."), status,
             data={"sent": email_result.get("sent", False),
                   "blocked_numbers": email_result.get("offending_numbers", [])})
    trace_store.finish_run(run_id)
    return {**result, "outcome": "variance", "match": record, "email": email_result,
            "extracted": extracted}


def propose_posting_for_match(match_id: str) -> dict:
    """STEP 2 of the invoice flow: a human has looked at a finished 3-way match
    and asked to post it. THIS is where the posting pre-flight (tier 1) runs.

    Splitting it from the match is the whole point: pre-flight judges whether
    SAP will accept a posting, which is a question about master data on the PO
    (payment terms, the GR-based-IV flag, deletion/release/final-invoice
    indicators) and not about whether the invoice is correct. Reporting those
    blockers to someone who only asked "does this match?" buries a clean result
    under problems they did not ask about and cannot act on. So they surface
    here, at the moment they are actually load-bearing, and the review queue is
    only involved once a real posting attempt has been blocked.

    Takes only a match_id - the figures come from the stored, server-computed
    match record, never from the caller (see invoice_matches.py). Writes
    nothing to SAP: it ends at a proposal, which still needs the human approval
    click that calls commit_post_supplier_invoice.

    Not exposed to any LLM tool list. The model may run a match; only a person
    can decide to move toward posting one.
    """
    deps = _deps()
    entry, reason = deps.matches.get(match_id)
    if entry is None:
        return {"ok": False, "error": reason}

    run_id, record, extracted = entry.run_id, entry.record, entry.extracted

    def emit(step, label, status="ok", tool="", data=None):
        trace_store.emit(run_id, step, label, status, tool, data)

    # One match, at most one live proposal. Minting a second for the same match
    # would create a second proposal_id, and the commit ledger's idempotency key
    # is (payload_hash, proposal_id) - two ids means two commits it would not
    # recognise as the same invoice.
    if entry.proposal_id:
        posted = deps.ledger.find_by_proposal(entry.proposal_id)
        if posted:
            return {"ok": False, "error": (
                f"This invoice was already posted as SupplierInvoice "
                f"{posted['supplier_invoice']}/{posted['fiscal_year']}. Do not post it again."
            )}
        existing, _expired = deps.proposals.claim(entry.proposal_id)
        if existing is not None:
            return {
                "ok": True, "run_id": run_id, "outcome": "proposed",
                "match": record, "extracted": extracted,
                "proposal": {
                    "ok": True,
                    "proposal_id": existing.proposal_id,
                    "summary": existing.summary,
                    "payload": existing.payload,
                    "fields": (build_proposal_fields(existing.proposed, existing.confidences)
                               if existing.proposed is not None else []),
                    "matched": bool(record.get("matched")),
                    "warnings": existing.warnings,
                },
            }
        # Expired out of the proposal store (and never posted) - safe to rebuild.

    emit("preflight", "Checking whether SAP will accept a posting for this match...",
         "running", "propose_post_supplier_invoice")
    field_confidences = {k: v.get("confidence") for k, v in extracted.items()}
    proposal = propose_post_supplier_invoice(match_record=record, confidences=field_confidences)

    if proposal.get("blocked"):
        # Tier 1 stopped this before SAP was contacted. Now that a posting was
        # actually requested, this is a real piece of work for a named person -
        # so it is parked in the review queue rather than only being reported
        # into an HTTP response that disappears when the drawer closes.
        review = deps.review_queue.enqueue({
            "reason": "posting_blocked",
            "detail": proposal.get("error", ""),
            "purchase_order": record.get("purchase_order", ""),
            "blockers": proposal.get("blockers", []),
            "extracted": extracted,
            "values": entry.values,
        })
        emit("preflight",
             "Pre-flight checks blocked this posting before SAP was called: "
             + "; ".join(b["message"] for b in proposal.get("blockers", [])),
             "warn", "propose_post_supplier_invoice",
             data={"blockers": proposal.get("blockers", []),
                   "codes": proposal.get("codes", []),
                   "review_id": review.get("id")})
        trace_store.finish_run(run_id)
        return {"ok": True, "run_id": run_id, "outcome": "blocked", "match": record,
                "proposal": proposal, "review": review, "extracted": extracted}

    if not proposal.get("ok"):
        emit("propose", f"Could not build a proposal: {proposal.get('error')}", "warn")
        trace_store.finish_run(run_id)
        return {"ok": True, "run_id": run_id, "outcome": "proposal_failed", "match": record,
                "proposal": proposal, "extracted": extracted}

    deps.matches.attach_proposal(match_id, proposal["proposal_id"])
    emit("propose", "Pre-flight passed. Built a posting proposal - awaiting human "
         "approval before commit.", "ok", "propose_post_supplier_invoice",
         {"proposal_id": proposal["proposal_id"]})
    trace_store.finish_run(run_id)
    return {"ok": True, "run_id": run_id, "outcome": "proposed", "match": record,
            "proposal": proposal, "extracted": extracted}


_RETRYABLE_REVIEW_REASONS = ("po_not_found", "po_line_not_found", "ambiguous_po_line")


def retry_match_with_corrected_po(review_id: str, corrected_po_number: str) -> dict:
    """Re-run the match against a parked review entry with a human-corrected
    PO number. Covers all three ways the match step can park an invoice for
    a PO-number problem (po_not_found / po_line_not_found / ambiguous_po_line)
    - each stores everything else the invoice needs (`values`, `extracted`)
    so only the PO number itself has to change.

    Mints its own run_id so the UI can show a fresh trace for the retry. A
    successful match resolves the review entry and continues exactly as
    run_invoice_pipeline's tail would (via _resume_after_match) - a corrected
    PO number is not a lower-risk path, so it lands on the same "matched, post
    separately" stop and the same approval gate as any other invoice. A repeat
    failure leaves the entry open so it can be tried again.
    """
    corrected = corrected_po_number.strip().replace("'", "")
    if not corrected:
        return {"ok": False, "error": "corrected_po_number is required."}

    entry = _deps().review_queue.get(review_id)
    if entry is None:
        return {"ok": False, "error": "Unknown review_id."}
    if entry.get("status") != "open":
        return {"ok": False, "error": "This review item was already resolved."}
    if entry.get("reason") not in _RETRYABLE_REVIEW_REASONS:
        return {"ok": False, "error": "This review item isn't a PO-match issue a "
                "corrected PO number can fix."}

    values = entry.get("values") or {}
    extracted = entry.get("extracted") or {}
    used_fallback = bool(entry.get("used_description_fallback"))
    po_item_for_match = "" if used_fallback else values.get("po_item", "")

    run_id = trace_store.start_run(None)
    trace_store.emit(run_id, "match", f"Retrying the match with corrected PO number "
                      f"{corrected}...", "running", "match_supplier_invoice")
    match = match_supplier_invoice(
        po_number=corrected,
        po_item=po_item_for_match,
        quantity=values.get("quantity", ""),
        unit_price=values.get("unit_price", ""),
        currency=values.get("currency", ""),
        vendor_name=values.get("vendor_name", ""),
        vendor_invoice_number=values.get("vendor_invoice_number", ""),
        material_description=values.get("material_description", ""),
    )
    if not match.get("ok"):
        trace_store.emit(run_id, "match", f"Match failed: {match.get('error')}", "error")
        trace_store.finish_run(run_id)
        return {"ok": False, "run_id": run_id, "outcome": "match_failed",
                "error": match.get("error"), "extracted": extracted,
                "still_not_found": bool(match.get("not_found"))}

    _deps().review_queue.resolve(review_id, resolution="po_corrected", corrected_po_number=corrected)
    return _resume_after_match(run_id, match["match"], values, extracted, used_fallback,
                                auto_send_email=True)


def run_invoice_pipeline(
    file_bytes: bytes,
    content_type: str,
    run_id: str | None = None,
    auto_send_email: bool = True,
) -> dict:
    """Sequence STEP 1 - the match - emitting one trace event per step for the
    live UI. Stops at `outcome: "matched"` for a clean match (posting is a
    separate request: propose_posting_for_match) and at a variance record +
    email for a mismatch. Runs no posting check and never commits."""
    run_id = trace_store.start_run(run_id)
    result: dict = {"ok": True, "run_id": run_id}

    def emit(step, label, status="ok", tool="", data=None):
        trace_store.emit(run_id, step, label, status, tool, data)

    # 1) Extract
    emit("extract", "Reading the invoice document...", "running", "extract_invoice_fields")
    extraction = extract_invoice_fields(file_bytes, content_type)
    if not extraction.get("ok"):
        emit("extract", f"Extraction failed: {extraction.get('error')}", "error")
        trace_store.finish_run(run_id)
        return {**result, "ok": False, "outcome": "extraction_failed", "error": extraction.get("error")}
    values = extraction["values"]
    emit("extract", "Extracted invoice fields.", "ok", "extract_invoice_fields", {
        "po_number": values.get("po_number", ""),
        "vendor_invoice_number": values.get("vendor_invoice_number", ""),
        "low_confidence": extraction["low_confidence"],
    })

    # 2) Confidence gate -> human review queue
    if extraction["needs_review"]:
        review = _deps().review_queue.enqueue({
            "reason": "low_confidence",
            "detail": extraction["review_reason"],
            "low_confidence_fields": extraction["low_confidence"],
            "blocking_fields": extraction["blocking_low_confidence"],
            "extracted": extraction["extracted"],
        })
        emit("review", extraction["review_reason"] + " - routed to human review "
             "(no guess).", "warn", "list_review_queue",
             data={"fields": extraction["low_confidence"],
                   "blocking_fields": extraction["blocking_low_confidence"],
                   "review_id": review.get("id")})
        trace_store.finish_run(run_id)
        return {**result, "outcome": "needs_review", "review": review,
                "low_confidence": extraction["low_confidence"], "extracted": extraction["extracted"]}

    # 2b) PO item unreadable, but the description was read well enough to try the
    # fallback. Clear the low-confidence number so find_po_line takes the
    # description route: feeding a misread item number into an exact match could
    # silently hit the WRONG line, which is worse than falling back.
    po_item_for_match = values.get("po_item", "")
    used_fallback = not extraction["po_item_readable"]
    if used_fallback:
        po_item_for_match = ""
        emit("fallback", "PO item number unreadable - matching on the material "
             "description instead (flagged, not treated as a direct hit).",
             "warn", data={"material_description": values.get("material_description", ""),
                           "po_item_confidence": extraction["extracted"]["po_item"]["confidence"]})

    # 3) Duplicate check (before PO lookup)
    emit("duplicate", "Checking for a duplicate invoice...", "running", "check_duplicate_invoice")
    dup = check_duplicate_invoice(values.get("vendor_name", ""), values.get("vendor_invoice_number", ""))
    if dup.get("is_duplicate"):
        emit("duplicate", "Duplicate invoice detected - stopping.", "warn",
             data={"existing": dup.get("existing", {})})
        trace_store.finish_run(run_id)
        return {**result, "outcome": "duplicate", "duplicate": dup, "extracted": extraction["extracted"]}
    emit("duplicate", "No duplicate found in the ledger.", "ok")

    # 4-6) PO lookup + GR sum + match
    emit("match", "Looking up the PO line, summing goods receipts, matching...",
         "running", "match_supplier_invoice")
    match = match_supplier_invoice(
        po_number=values.get("po_number", ""),
        po_item=po_item_for_match,
        quantity=values.get("quantity", ""),
        unit_price=values.get("unit_price", ""),
        currency=values.get("currency", ""),
        vendor_name=values.get("vendor_name", ""),
        vendor_invoice_number=values.get("vendor_invoice_number", ""),
        material_description=values.get("material_description", ""),
    )
    if not match.get("ok"):
        if match.get("not_found"):
            # A 404 on the PO number itself is very often a misread digit
            # (see server.py's _fetch_po_status) - park it with everything
            # needed to retry, instead of a dead-end error the user can only
            # read and re-upload from scratch.
            review = _deps().review_queue.enqueue({
                "reason": "po_not_found",
                "detail": match.get("error", ""),
                "attempted_po_number": values.get("po_number", ""),
                "used_description_fallback": used_fallback,
                "extracted": extraction["extracted"],
                "values": values,
            })
            emit("review", match.get("error", "PO number not found on this tenant.") +
                 " - routed to human review so the PO number can be corrected and retried.",
                 "warn", "list_review_queue", data={"review_id": review.get("id")})
            trace_store.finish_run(run_id)
            return {**result, "outcome": "po_not_found", "review": review,
                    "extracted": extraction["extracted"]}
        emit("match", f"Match failed: {match.get('error')}", "error")
        trace_store.finish_run(run_id)
        return {**result, "ok": False, "outcome": "match_failed", "error": match.get("error"),
                "extracted": extraction["extracted"]}

    return _resume_after_match(run_id, match["match"], values, extraction["extracted"],
                                used_fallback, auto_send_email)

"""Tier 1 pre-flight + tier 2 error translation, and the two SAP rejections
they exist to prevent.

These tests deliberately run the REAL parser into the REAL matcher into the
REAL pre-flight into the REAL payload builder wherever a bug lived at a seam
between two of them. This codebase's recurring failure mode is two halves of a
feature written to different assumptions - each correct alone, so nothing
raises and no test fails, and the feature is simply unreachable. Asserting on a
hand-built dict in the middle would reproduce exactly that blind spot, so the
seam tests below start from a raw OData body.
"""

from __future__ import annotations

import pytest

from factories import gr_row, invoice, odata_po_v4, po_item, po_status
from app.mcp.invoice_matching import Tolerances, match_invoice
from app.mcp.invoice_posting import ProposedInvoice, ProposedInvoiceItem, build_supplier_invoice_payload
from app.mcp.invoice_preflight import (
    GR_BASED_IV_UNKNOWN,
    GR_REFERENCE_MISSING,
    INVOICE_NOT_EXPECTED,
    ITEM_FINALLY_INVOICED,
    PAYMENT_TERMS_MISSING,
    PO_ITEM_DELETED,
    PO_RELEASE_INCOMPLETE,
    REFERENCE_MODE_GR,
    REFERENCE_MODE_PO,
    SERVICE_BASED_IV,
    blocked_result,
    collect_prerequisites,
    run_preflight,
)
from app.mcp.po import parse_po_status

REFERENCE_KEYS = ("ReferenceDocument", "ReferenceDocumentFiscalYear", "ReferenceDocumentItem")


def _prereqs(po=None, line=None, gr=None) -> dict:
    """collect_prerequisites over healthy defaults, so each test overrides only
    the fact it is about."""
    status = po_status() if po is None else po
    item = status["items"][0] if line is None else line
    reference = {"document": "5000000123", "fiscal_year": "2026", "item": "1"} if gr is None else gr
    return collect_prerequisites(status, item, reference)


def _proposed(**item_overrides) -> ProposedInvoice:
    defaults = dict(
        supplier_invoice_item="1", purchase_order="4500000002", purchase_order_item="10",
        quantity="10", unit="PC", amount="250.00", tax_code="V0",
        gr_document="5000000123", gr_fiscal_year="2026", gr_item="1",
    )
    defaults.update(item_overrides)
    return ProposedInvoice(
        company_code="M100", document_date="2026-07-29", posting_date="2026-07-29",
        tax_determination_date="2026-07-29", invoicing_party="TST02",
        vendor_invoice_number="INV-0042", currency="IDR", gross_amount="250.00",
        items=(ProposedInvoiceItem(**defaults),),
    )


# ---- BUG 1: the reference shape -------------------------------------------


def test_po_based_item_omits_the_goods_receipt_reference_entirely():
    """THE BUG. GR-based IV off means the invoice item references the PO line
    only. The old code always sent the GR triple, which SAP rejects with "Only
    fill ReferenceDocument/-FiscalYear/-Item if GR-based IV is active".

    The keys must be ABSENT, not blank: SAP's check is on their presence, so
    sending them as empty strings would fail the same way.
    """
    payload = build_supplier_invoice_payload(_proposed(reference_mode=REFERENCE_MODE_PO))
    line = payload["to_SuplrInvcItemPurOrdRef"][0]

    for key in REFERENCE_KEYS:
        assert key not in line, (
            f"{key} was sent on a PO-based item. SAP rejects this with 'Only fill "
            "ReferenceDocument/-FiscalYear/-Item if GR-based IV is active' plus "
            "'Item not selectable' - this is the exact bug being fixed."
        )
    assert line["PurchaseOrder"] == "4500000002"
    assert line["PurchaseOrderItem"] == "10"


def test_gr_based_item_still_sends_the_goods_receipt_reference():
    """The other half of the same branch. Fixing the PO-based case must not
    break GR-based posting, where SAP's MRM_FRSEG_CHECK REQUIRES the triple."""
    payload = build_supplier_invoice_payload(_proposed(reference_mode=REFERENCE_MODE_GR))
    line = payload["to_SuplrInvcItemPurOrdRef"][0]

    assert line["ReferenceDocument"] == "5000000123"
    assert line["ReferenceDocumentFiscalYear"] == "2026"
    assert line["ReferenceDocumentItem"] == "1"
    # The PO reference stays too - the GR triple is additional, not a swap.
    assert line["PurchaseOrder"] == "4500000002"


def test_payload_refuses_to_build_without_a_resolved_reference_mode():
    """An unset mode must raise rather than default. Defaulting either way
    reintroduces the bug silently: the payload still looks well-formed and only
    SAP finds out."""
    with pytest.raises(ValueError, match="reference_mode"):
        build_supplier_invoice_payload(_proposed())


def test_reference_shape_is_decided_end_to_end_from_the_raw_odata_body():
    """SEAM TEST - the one that matters most.

    Raw OData -> parse_po_status -> match_invoice -> collect_prerequisites ->
    run_preflight -> build_supplier_invoice_payload, with nothing hand-built in
    between. Any link that stops carrying the flag (a parser that drops the
    field, a matcher that doesn't forward it, a builder that ignores it) breaks
    HERE rather than silently reverting to the old always-GR behaviour.
    """
    parsed = parse_po_status(odata_po_v4())
    record = match_invoice(
        invoice=invoice(), po_status=parsed, gr_rows=[gr_row()], tolerances=Tolerances(),
    )
    assert record["posting_prerequisites"]["gr_based_iv"] is False, (
        "The GR-based IV flag did not survive parse_po_status -> match_invoice. "
        "Check that parse_po_status maps InvoiceIsGoodsReceiptBased and that "
        "match_invoice still calls collect_prerequisites."
    )

    preflight = run_preflight(record["posting_prerequisites"])
    assert not preflight.blocked, preflight.summary()
    assert preflight.reference_mode == REFERENCE_MODE_PO

    payload = build_supplier_invoice_payload(
        _proposed(reference_mode=preflight.reference_mode)
    )
    assert not any(k in payload["to_SuplrInvcItemPurOrdRef"][0] for k in REFERENCE_KEYS)


def test_gr_based_flag_survives_the_same_end_to_end_path():
    """The mirror of the seam test above: a tenant whose PO line IS GR-based
    must still reach REFERENCE_MODE_GR through the identical chain."""
    body = odata_po_v4(items=[{
        "PurchaseOrderItem": "10", "Material": "EGM002",
        "PurchaseOrderItemText": "Hex bolts M8 stainless", "OrderQuantity": "10",
        "PurchaseOrderQuantityUnit": "PC", "NetPriceAmount": "25.00",
        "InvoiceIsGoodsReceiptBased": True, "InvoiceIsExpected": True,
        "IsFinallyInvoiced": False,
    }])
    record = match_invoice(
        invoice=invoice(), po_status=parse_po_status(body), gr_rows=[gr_row()],
        tolerances=Tolerances(),
    )
    preflight = run_preflight(record["posting_prerequisites"])

    assert preflight.reference_mode == REFERENCE_MODE_GR
    assert not preflight.blocked, preflight.summary()


def test_unknown_gr_based_iv_flag_blocks_instead_of_guessing():
    """A tenant that does not return InvoiceIsGoodsReceiptBased leaves the
    reference shape undecidable. Blocking is the point: either guess is a
    rejection, so there is no safe default to fall back on."""
    body = odata_po_v4(items=[{
        "PurchaseOrderItem": "10", "Material": "EGM002", "OrderQuantity": "10",
        # InvoiceIsGoodsReceiptBased deliberately absent
    }])
    parsed = parse_po_status(body)
    assert parsed["items"][0]["gr_based_iv"] is None, (
        "An absent flag must parse to None, not False - False is a real answer "
        "that selects the PO-based shape."
    )

    preflight = run_preflight(_prereqs(po=parsed, line=parsed["items"][0]))
    assert preflight.blocked
    assert GR_BASED_IV_UNKNOWN in [b["code"] for b in preflight.blockers]
    assert preflight.reference_mode == "", "No mode may be reported when the flag is unknown."


def test_empty_string_is_not_read_as_false():
    """Guards the tri-state. An empty value is 'not sent', not 'off' - reading
    it as False would silently select the PO-based shape on a GR-based line."""
    parsed = parse_po_status(odata_po_v4(items=[{
        "PurchaseOrderItem": "10", "OrderQuantity": "10",
        "InvoiceIsGoodsReceiptBased": "",
    }]))
    assert parsed["items"][0]["gr_based_iv"] is None


def test_gr_based_line_without_a_goods_receipt_is_blocked():
    """GR-based IV on, but no usable receipt: SAP would reject the empty
    reference. Caught locally instead."""
    line = po_item(gr_based_iv=True)
    preflight = run_preflight(
        _prereqs(line=line, gr={"document": "", "fiscal_year": "", "item": ""})
    )
    assert preflight.blocked
    assert GR_REFERENCE_MISSING in [b["code"] for b in preflight.blockers]


def test_po_based_line_with_a_receipt_warns_but_does_not_block():
    """A goods receipt on a PO-based line is normal. It must not be referenced,
    and must not stop the posting either."""
    preflight = run_preflight(_prereqs())
    assert not preflight.blocked
    assert any("not referenced" in w["message"].lower() for w in preflight.warnings)


# ---- BUG 2: payment terms --------------------------------------------------


def test_missing_payment_terms_blocks_before_sap_is_called():
    preflight = run_preflight(_prereqs(po=po_status(payment_terms="")))

    assert preflight.blocked
    assert PAYMENT_TERMS_MISSING in [b["code"] for b in preflight.blockers]

    result = blocked_result(preflight, purchase_order="4500000002")
    assert result["ok"] is False
    assert result["sap_called"] is False, (
        "The whole promise of tier 1 is that SAP is never contacted for a "
        "locally-detectable failure."
    )
    assert "proposal_id" not in result, "A blocked posting must not mint an approvable proposal."


def test_payment_terms_blocker_names_the_fix_and_invents_no_value():
    """The brief's 'surface, don't guess'. The reason must point at the vendor
    master / PO, and no payment-terms value may be fabricated anywhere."""
    preflight = run_preflight(_prereqs(po=po_status(payment_terms="")))
    blocker = next(b for b in preflight.blockers if b["code"] == PAYMENT_TERMS_MISSING)

    remedy = blocker["remedy"].lower()
    assert "vendor master" in remedy
    assert "purchase order" in blocker["message"].lower()


def test_payment_terms_present_does_not_block():
    assert not run_preflight(_prereqs(po=po_status(payment_terms="0001"))).blocked


def test_payment_terms_survive_the_parser():
    """Seam: the blocker is only reachable if parse_po_status actually maps
    PaymentTerms. A check reading a key nobody writes can never fire - gap 3
    and gap 6 in this codebase were both exactly that."""
    assert parse_po_status(odata_po_v4())["payment_terms"] == "0001"
    assert parse_po_status(odata_po_v4(PaymentTerms=""))["payment_terms"] == ""


# ---- the other PO-item prerequisites --------------------------------------


@pytest.mark.parametrize("overrides,expected_code", [
    ({"invoice_expected": False}, INVOICE_NOT_EXPECTED),
    ({"finally_invoiced": True}, ITEM_FINALLY_INVOICED),
    ({"service_based_iv": True}, SERVICE_BASED_IV),
    ({"deleted": True}, PO_ITEM_DELETED),
])
def test_item_level_prerequisites_block_with_distinct_reasons(overrides, expected_code):
    """Each failure gets its OWN code and sentence - the brief's "not a generic
    'invoice invalid' message"."""
    preflight = run_preflight(_prereqs(line=po_item(**overrides)))
    codes = [b["code"] for b in preflight.blockers]

    assert expected_code in codes
    blocker = next(b for b in preflight.blockers if b["code"] == expected_code)
    assert blocker["message"] and blocker["remedy"]


def test_incomplete_release_blocks():
    preflight = run_preflight(_prereqs(po=po_status(release_incomplete=True)))
    assert PO_RELEASE_INCOMPLETE in [b["code"] for b in preflight.blockers]


def test_every_blocker_message_is_distinct_and_actionable():
    """No generic message, and every blocker names a remedy. A blocker a person
    cannot act on is a dead end."""
    preflight = run_preflight(collect_prerequisites(
        po_status(payment_terms="", release_incomplete=True),
        po_item(invoice_expected=False, finally_invoiced=True, gr_based_iv=None),
        {"document": "", "fiscal_year": "", "item": ""},
    ))
    messages = [b["message"] for b in preflight.blockers]

    assert len(messages) == len(set(messages)), "Two blockers share a message."
    assert len(preflight.blockers) >= 4, "Independent failures must all be reported at once."
    for blocker in preflight.blockers:
        assert blocker["remedy"].strip(), f"{blocker['code']} has no remedy."
        assert "invalid" != blocker["message"].strip().lower()


def test_a_match_record_without_prerequisites_blocks_rather_than_posting():
    """Defensive: an old or hand-built match record carries no prerequisites.
    That must fail closed - the missing facts read as 'unknown', which blocks."""
    preflight = run_preflight({})
    assert preflight.blocked
    codes = [b["code"] for b in preflight.blockers]
    assert GR_BASED_IV_UNKNOWN in codes and PAYMENT_TERMS_MISSING in codes


def test_editing_a_proposal_preserves_the_reference_mode():
    """A human edit rebuilds the payload, and the builder now RAISES on a lost
    reference_mode - so if apply_field_edits dropped it, editing any field at
    all would break posting. Pinned here because the two live in different
    modules and neither's own tests would notice."""
    from app.mcp.invoice_posting import apply_field_edits

    proposed = _proposed(reference_mode=REFERENCE_MODE_PO)
    updated, rejected = apply_field_edits(proposed, {"item_1_quantity": "5"})

    assert updated.items[0].reference_mode == REFERENCE_MODE_PO
    assert updated.items[0].quantity == "5"
    payload = build_supplier_invoice_payload(updated)   # must not raise
    assert not any(k in payload["to_SuplrInvcItemPurOrdRef"][0] for k in REFERENCE_KEYS)


def test_reference_rows_in_the_approval_ui_are_locked():
    """The reference basis is SAP-determined. It is shown so an approver can see
    which shape was chosen, but it must not be editable - hand-editing it would
    detach the posting from the flag that decided it."""
    from app.mcp.invoice_posting import build_proposal_fields

    fields = build_proposal_fields(_proposed(reference_mode=REFERENCE_MODE_GR))
    reference_rows = [f for f in fields if "reference" in f["key"]]

    assert reference_rows, "The approval UI shows no reference-basis row."
    assert all(not f["editable"] for f in reference_rows)


def _matched_run_on_a_po_without_payment_terms(monkeypatch, tmp_path):
    """Drive the REAL step 1 to a clean 3-way match whose PO cannot be posted
    against (no payment terms). Returns (deps, step-1 result)."""
    from app.mcp import invoice_tools
    from app.mcp.invoice_review import ReviewQueue

    deps = invoice_tools._deps()
    monkeypatch.setattr(deps, "review_queue", ReviewQueue(tmp_path / "queue.json"))
    # The duplicate checks would hit SAP; these tests are about the posting fork.
    monkeypatch.setattr(invoice_tools, "check_duplicate_by_supplier_code",
                        lambda *a, **k: {"ok": True, "is_duplicate": False})
    monkeypatch.setattr(invoice_tools, "check_duplicate_in_sap",
                        lambda *a, **k: {"ok": True, "is_duplicate": False})

    record = match_invoice(
        invoice=invoice(),
        po_status=parse_po_status(odata_po_v4(PaymentTerms="")),
        gr_rows=[gr_row()],
        tolerances=Tolerances(),
    )
    assert record["matched"] is True, "Precondition: this is a genuinely clean 3-way match."

    result = invoice_tools._resume_after_match(
        run_id="test-run", record=record, values=invoice(), extracted={},
        used_fallback=False, auto_send_email=False,
    )
    return deps, result


def test_matching_alone_never_runs_the_posting_preflight(monkeypatch, tmp_path):
    """THE SPLIT. Step 1 answers "does this invoice match?" - a question about
    the invoice. Pre-flight answers "will SAP accept a posting?" - a question
    about master data on the PO. Asking the first must not answer the second:
    a perfect match on a PO with no payment terms is still a perfect match, and
    burying it under blockers the user never asked about (and cannot fix from
    here) is what this two-step flow exists to stop.
    """
    deps, result = _matched_run_on_a_po_without_payment_terms(monkeypatch, tmp_path)

    assert result["outcome"] == "matched", (
        "A clean match came back as something other than 'matched' - step 1 is "
        "still running the posting checks."
    )
    assert "proposal" not in result, "Step 1 built a posting proposal; posting is step 2's job."
    assert result.get("match_id"), (
        "No match_id was handed back, so there is no way to reach step 2 - the "
        "posting path is unreachable."
    )
    assert not deps.review_queue.list_open(), (
        "A clean match was parked for human review. Nothing needs a human here: "
        "the invoice matched and no posting was attempted."
    )


def test_posting_step_reports_blocked_and_parks_it_for_a_human(monkeypatch, tmp_path):
    """REACHABILITY, the other half. A branch that exists but no input can reach
    is this codebase's signature failure - so this continues the SAME run into
    step 2 and requires the blocked outcome to come out the far end and be
    queued, not just to exist in the source.
    """
    from app.mcp import invoice_tools

    deps, matched = _matched_run_on_a_po_without_payment_terms(monkeypatch, tmp_path)
    result = invoice_tools.propose_posting_for_match(matched["match_id"])

    assert result["outcome"] == "blocked", (
        "Asking to post a clean match on a PO with no payment terms did not "
        "surface as 'blocked'. The pre-flight branch is unreachable."
    )
    assert result["proposal"]["sap_called"] is False
    assert PAYMENT_TERMS_MISSING in result["proposal"]["codes"]
    assert deps.review_queue.list_open(), (
        "The blocked posting was not parked for a human - now that someone has "
        "actually asked to post, there is real work for a named person."
    )


def test_the_posting_step_is_not_reachable_from_any_model_tool():
    """The pre-flight/propose step is a human's decision. If it ever lands in a
    tool list, a model can start moving an invoice toward SAP on its own - and
    the narration table would then also need entries it deliberately lacks."""
    from app.mcp.tools import _DISPATCH, build_tool_specs

    names = {s["toolSpec"]["name"] for s in build_tool_specs(has_attachment=True)}
    assert "propose_posting_for_match" not in names | set(_DISPATCH)


def test_matched_outcome_has_a_narration_entry():
    """Same rule the seam test enforces for every other outcome: a bare enum
    with no message lets Nova invent what it means - here, that a clean match
    means the invoice is on its way into SAP."""
    from app.mcp.tools import _OUTCOME_MESSAGES

    assert "matched" in _OUTCOME_MESSAGES
    text = _OUTCOME_MESSAGES["matched"].lower()
    assert "nothing has been posted" in text, "The narration must say nothing was posted."


# ---- TIER 2: error translation --------------------------------------------


class _StubProvider:
    """Records what it was asked, so the test can assert on the boundary."""

    def __init__(self, answer="Plain language explanation.", fail=False):
        self.answer = answer
        self.fail = fail
        self.calls = []

    def generate(self, system_prompt, user_prompt, max_tokens):
        self.calls.append({"system": system_prompt, "user": user_prompt, "max_tokens": max_tokens})
        if self.fail:
            raise RuntimeError("Bedrock unavailable")

        class _Result:
            answer = self.answer

        return _Result()


def test_known_sap_errors_are_translated_without_calling_the_model():
    """Tier 2 is a fallback. Anything already understood is answered
    deterministically - identical wording every time, and no tokens spent."""
    from app.mcp.invoice_error_translation import translate_posting_error

    provider = _StubProvider()
    result = translate_posting_error(
        "SAP rejected posting the supplier invoice: Only fill ReferenceDocument/"
        "-FiscalYear/-Item if GR-based IV is active",
        provider=provider,
    )
    assert result["source"] == "known"
    assert provider.calls == [], "A mapped error must not reach the LLM."


def test_unmapped_error_goes_to_the_model_once_and_is_bounded():
    from app.mcp.invoice_error_translation import translate_posting_error

    provider = _StubProvider(answer="The cost centre on the order is locked.")
    result = translate_posting_error(
        "SAP rejected posting the supplier invoice: cost centre 4711 is locked",
        raw_body="cost centre 4711 is locked for postings",
        provider=provider,
    )

    assert result["source"] == "llm"
    assert result["explanation"] == "The cost centre on the order is locked."
    assert len(provider.calls) == 1, "Tier 2 is ONE call - no retry loop."
    assert provider.calls[0]["max_tokens"] <= 300


def test_translation_failure_degrades_to_saps_own_message():
    """A UX layer must never be able to turn a clear failure into an opaque
    one, so every failure path returns the untranslated text."""
    from app.mcp.invoice_error_translation import translate_posting_error

    original = "SAP rejected posting the supplier invoice: something unmapped"
    result = translate_posting_error(original, provider=_StubProvider(fail=True))

    assert result["explanation"] == original
    assert result["source"] == "untranslated"


def test_translation_works_with_no_provider_at_all():
    from app.mcp.invoice_error_translation import translate_posting_error

    result = translate_posting_error("some unmapped SAP text", provider=None)
    assert result["source"] == "untranslated"


def test_the_model_is_never_shown_anything_it_could_act_on():
    """Structural guarantee, not a prompt promise: the translator is handed
    error TEXT only. No payload, no proposal id, no client - so there is
    nothing for a translation to modify or resubmit even in principle."""
    import inspect

    from app.mcp import invoice_error_translation

    signature = inspect.signature(invoice_error_translation.translate_posting_error)
    assert set(signature.parameters) == {"sap_message", "raw_body", "provider"}

    provider = _StubProvider()
    translate = invoice_error_translation.translate_posting_error
    translate("unmapped error text", raw_body="unmapped raw", provider=provider)
    sent = provider.calls[0]["user"] + provider.calls[0]["system"]
    for forbidden in ("proposal_id", "to_SuplrInvcItemPurOrdRef", "A_SupplierInvoice"):
        assert forbidden not in sent


def test_error_translation_has_no_import_path_to_the_writer():
    """The commit tool must be unreachable from the translation layer. This
    asserts on the module's imports rather than trusting the prompt: tier 2 may
    explain a failure, never cause a write."""
    import ast
    from pathlib import Path

    source = Path("app/mcp/invoice_error_translation.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert not any("invoice_tools" in module for module in imported), (
        "invoice_error_translation imports invoice_tools - that creates a path "
        "from the LLM fallback to commit_post_supplier_invoice."
    )

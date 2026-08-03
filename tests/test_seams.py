"""Seam contract tests - the bug class this codebase actually produces.

Every gap found in the audits of 2026-07-26 and 2026-07-27 had the same shape:
two halves of one feature written to slightly different assumptions. Each half
was correct in isolation, nothing raised, no test failed - the feature was just
silently unreachable, and the pipeline's own "route it to a human" behaviour
made the failure look like a policy decision.

Ordinary unit tests cannot catch that, because each half passes its own tests.
These tests assert the CONTRACT BETWEEN halves: the key one side writes is the
key the other side reads, the branch below a gate is reachable given that gate,
the enum one side emits is in the table the other side looks up.

Each test names the gap it descends from. When one fails, it is telling you a
feature has silently stopped working - not that a detail changed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"


def _module_ast(relative_path: str) -> ast.Module:
    return ast.parse((APP / relative_path).read_text(encoding="utf-8"))


def _outcome_literals(tree: ast.Module) -> set[str]:
    """Every string literal this module can assign to an `outcome`.

    Covers the three forms in use: a dict entry {"outcome": "x"}, a bare
    assignment `outcome = "x"`, and a conditional `"a" if cond else "b"` in
    either position. Reading the source rather than a hand-kept list is the
    point - a new branch added tomorrow is picked up without anyone
    remembering to update a test.
    """
    found: set[str] = set()

    def literals_in(node: ast.AST) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            return literals_in(node.body) | literals_in(node.orelse)
        return set()  # a Subscript/Name is a passthrough, not a new outcome

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "outcome":
                    found |= literals_in(value)
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "outcome" for t in node.targets):
                found |= literals_in(node.value)
    return found


# Outcomes that exist in the pipeline modules but can never come back from
# run_supplier_invoice_match, so no model ever narrates them:
#   "clean"    - match_invoice's own verdict; _resume_after_match remaps it to
#                "matched" before returning.
#   the rest   - produced only by propose_posting_for_match, which is step 2 and
#                is reachable only from POST /api/invoice/propose/{match_id}, a
#                human clicking "Continue to posting". It is in no tool list
#                (test_invoice_preflight.py pins that). If it ever becomes one,
#                delete it from this set and give it a message in
#                tools._OUTCOME_MESSAGES.
_REMAPPED_BEFORE_THE_TOOL_LAYER = {"clean"}
_HUMAN_ONLY_POSTING_STEP = {"proposed", "blocked", "proposal_failed"}
_NEVER_REACHES_THE_TOOL_LAYER = _REMAPPED_BEFORE_THE_TOOL_LAYER | _HUMAN_ONLY_POSTING_STEP


def test_every_pipeline_outcome_has_a_narration_message():
    """GAP 8. The tool result reaches Nova as an enum plus a `message` that
    says what it MEANS. An outcome missing from that table arrives as a bare
    token and the model improvises - which is how it once asked the user for a
    PO number it had already read off the invoice.

    This is the test that would have caught `po_not_found` sitting outside
    _OUTCOME_MESSAGES while being fully reachable in the pipeline.
    """
    from app.mcp.tools import _OUTCOME_MESSAGES

    emitted = (
        _outcome_literals(_module_ast("mcp/invoice_tools.py"))
        | _outcome_literals(_module_ast("mcp/invoice_matching.py"))
    ) - _NEVER_REACHES_THE_TOOL_LAYER

    missing = sorted(emitted - set(_OUTCOME_MESSAGES))
    assert not missing, (
        f"Pipeline can return {missing}, but tools._OUTCOME_MESSAGES has no entry "
        "for it. Nova will be handed a bare enum and will invent what it means."
    )


def test_parser_emits_the_description_the_fallback_reads():
    """GAPS 1 and 6 - the same feature broken at two different seams.

    parse_po_status must populate a key that find_po_line actually searches.
    Asserting the key exists is not enough: gap 6 was a populated `material`
    (a CODE) being compared against invoice prose. So this runs the real
    parser output into the real matcher and requires a match on TEXT alone.
    """
    from factories import odata_po_v4
    from app.mcp.invoice_matching import find_po_line
    from app.mcp.po import parse_po_status

    parsed = parse_po_status(odata_po_v4())
    result = find_po_line(parsed["items"], "", "hex bolts stainless")

    assert result["found"] is True, (
        "The description fallback cannot match text the parser is emitting. "
        "Check that parse_po_status still maps PurchaseOrderItemText to "
        "'description' and that find_po_line still reads that key."
    )
    assert result["method"] == "material-description-fallback"


def test_parser_description_survives_the_v2_and_product_name_dialects():
    """Same seam, other tenants: the parser accepts three field names for the
    line text. If a fallback chain is dropped, the description route dies
    silently on whichever tenant used that dialect."""
    from factories import odata_po_v4
    from app.mcp.po import parse_po_status

    for field in ("PurchaseOrderItemText", "ProductName", "MaterialDescription"):
        body = odata_po_v4(items=[{
            "PurchaseOrderItem": "10",
            "Material": "EGM002",
            field: "Hex bolts M8 stainless",
            "OrderQuantity": "10",
        }])
        parsed = parse_po_status(body)
        assert parsed["items"][0]["description"] == "Hex bolts M8 stainless", (
            f"Line text from '{field}' was dropped by parse_po_status."
        )


def test_ledger_records_the_key_the_duplicate_check_reads(tmp_path):
    """GAP 3. The duplicate check could never fire: entries were recorded under
    the PO's supplier CODE while find_duplicate looks up by the extracted
    vendor TEXT. Both identities must round-trip, because they become available
    at different moments and catch different resubmissions."""
    from app.mcp.invoice_ledger import SupplierInvoiceLedger

    ledger = SupplierInvoiceLedger(tmp_path / "ledger.json")
    ledger.record(
        payload_hash="abc123",
        proposal_id="prop-1",
        vendor="Acme Industrial Ltd",     # the OCR'd text
        vendor_invoice_number="INV-0042",
        supplier_invoice="5105600000",
        fiscal_year="2026",
        invoicing_party="TST02",          # the resolved supplier code
        purchase_order="4500000002",
    )

    assert ledger.find_duplicate("Acme Industrial Ltd", "INV-0042") is not None, (
        "Recorded under a key find_duplicate cannot see - the duplicate check "
        "is dead and the same invoice can be posted twice."
    )
    assert ledger.find_duplicate_by_party("TST02", "INV-0042") is not None, (
        "The supplier-code identity does not round-trip; a resubmission whose "
        "vendor name OCR'd differently will not be caught."
    )


def test_duplicate_identity_tolerates_ocr_noise(tmp_path):
    """The vendor text comes off a photo, so the key must fold case, spacing
    and punctuation - otherwise the check only fires on a byte-identical read,
    which is the case that never happens in practice."""
    from app.mcp.invoice_ledger import SupplierInvoiceLedger

    ledger = SupplierInvoiceLedger(tmp_path / "ledger.json")
    ledger.record(
        payload_hash="abc123", proposal_id="prop-1",
        vendor="Acme Corp.", vendor_invoice_number="INV-0042",
        supplier_invoice="5105600000", fiscal_year="2026",
    )
    assert ledger.find_duplicate("ACME  CORP", "inv 0042") is not None


def test_recoverable_field_does_not_trip_the_blocking_gate():
    """GAP 1. po_item sat in the gate that stops the pipeline, so an unreadable
    item number returned before the matcher ever ran - making the description
    fallback built for exactly that case unreachable."""
    from app.mcp.invoice_extraction import (
        BLOCKING_FIELDS,
        CRITICAL_FIELDS,
        RECOVERABLE_FIELDS,
    )

    assert set(BLOCKING_FIELDS).isdisjoint(RECOVERABLE_FIELDS), (
        "A field is both blocking and recoverable - its recovery route is "
        "unreachable because the gate returns first."
    )
    assert "po_item" in RECOVERABLE_FIELDS
    assert set(CRITICAL_FIELDS) == set(BLOCKING_FIELDS) | set(RECOVERABLE_FIELDS)


def test_low_confidence_po_item_reaches_the_matcher():
    """The behavioural half of the gap above: a badly-read item number must be
    reported as low-confidence yet still NOT block, so the fallback can run."""
    from app.mcp.invoice_extraction import _to_extracted

    extracted = _to_extracted({
        "po_number": {"value": "4500000002", "confidence": 0.98},
        "po_item": {"value": "", "confidence": 0.10},
        "quantity": {"value": "10", "confidence": 0.95},
        "unit_price": {"value": "25.00", "confidence": 0.95},
        "material_description": {"value": "Hex bolts M8 stainless", "confidence": 0.90},
    })
    assert "po_item" in extracted.low_confidence_fields(0.75)
    assert extracted.blocking_low_confidence_fields(0.75) == [], (
        "An unreadable po_item is blocking the pipeline again - the "
        "description fallback is unreachable."
    )
    assert extracted.has_description_fallback(0.75) is True


def test_every_boto3_client_is_given_explicit_credentials():
    """GAP 7. The SES client was constructed with no keys while every other
    client passed settings.aws_*. Values in .env are not process env vars, so
    that client only worked on a machine with ~/.aws - it failed nowhere
    visible and silently sent no mail.

    A static check, because the failure only shows up on a differently
    configured machine and never in a unit test.
    """
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "client"):
                continue
            if not (isinstance(func.value, ast.Name) and func.value.id == "boto3"):
                continue
            kwargs = {kw.arg for kw in node.keywords}
            if not {"aws_access_key_id", "aws_secret_access_key"} <= kwargs:
                service = (
                    node.args[0].value if node.args
                    and isinstance(node.args[0], ast.Constant) else "?"
                )
                offenders.append(
                    f"{path.relative_to(APP.parent)}:{node.lineno} boto3.client('{service}')"
                )

    assert not offenders, (
        "These boto3 clients take no explicit credentials, so they fall back to "
        "the ambient AWS chain and work only on a machine with ~/.aws:\n  "
        + "\n  ".join(offenders)
    )


def test_every_advertised_tool_can_actually_be_dispatched():
    """A tool named in the spec list but missing from the dispatch table
    answers every call with 'Unknown tool' - the model keeps retrying a tool
    that structurally cannot run."""
    from app.mcp.tools import _DISPATCH, INVOICE_TOOL_SPEC, build_tool_specs

    advertised = {s["toolSpec"]["name"] for s in build_tool_specs(has_attachment=True)}
    runnable = set(_DISPATCH) | {INVOICE_TOOL_SPEC["toolSpec"]["name"]}
    assert advertised <= runnable, f"Advertised but not dispatchable: {advertised - runnable}"


def test_every_advertised_tool_is_described_in_the_system_prompt():
    """The prompt is what teaches Nova WHEN to reach for each tool. A tool
    present in the spec list but absent from the prompt is reachable in theory
    and unused in practice."""
    from app.generation.prompts import SAP_MCP_SYSTEM_PROMPT
    from app.mcp.tools import build_tool_specs

    missing = [
        spec["toolSpec"]["name"]
        for spec in build_tool_specs(has_attachment=True)
        if spec["toolSpec"]["name"] not in SAP_MCP_SYSTEM_PROMPT
    ]
    assert not missing, f"Tools the system prompt never mentions: {missing}"


def test_retryable_review_reasons_are_all_actually_producible():
    """The review queue offers a PO-number correction for three reasons. A
    reason listed there but never emitted is a dead UI affordance; one emitted
    but not listed parks an invoice a human cannot retry."""
    from app.mcp.invoice_tools import _RETRYABLE_REVIEW_REASONS

    producible = (
        _outcome_literals(_module_ast("mcp/invoice_tools.py"))
        | _outcome_literals(_module_ast("mcp/invoice_matching.py"))
    )
    missing = set(_RETRYABLE_REVIEW_REASONS) - producible
    assert not missing, f"Retryable reasons the pipeline never produces: {missing}"


# ---- safety boundaries ---------------------------------------------------
# Not seams between halves, but the invariants the whole design rests on. They
# are one careless edit away from silently disappearing, and nothing else in
# the suite would notice.


@pytest.mark.parametrize("has_attachment", [True, False])
def test_write_tools_are_never_exposed_to_the_chat_model(has_attachment):
    """create_purchase_order and commit_post_supplier_invoice are the only two
    functions that write to SAP. Neither may EVER appear in a tool spec: their
    safety depends on a human confirming first, which a freeform tool-calling
    turn cannot guarantee."""
    from app.mcp.tools import build_tool_specs

    names = {s["toolSpec"]["name"] for s in build_tool_specs(has_attachment=has_attachment)}
    forbidden = {"create_purchase_order", "commit_post_supplier_invoice",
                 "preview_purchase_order"}
    assert not (names & forbidden), (
        f"A write-capable tool is exposed to the chat model: {names & forbidden}"
    )


def test_invoice_tool_appears_only_when_a_file_is_attached():
    """Routing between 'match this invoice' and 'look up this PO' is enforced
    by the tool list, not by hoping the prompt holds. With no attachment the
    tool must not exist at all."""
    from app.mcp.tools import build_tool_specs

    without = {s["toolSpec"]["name"] for s in build_tool_specs(has_attachment=False)}
    with_file = {s["toolSpec"]["name"] for s in build_tool_specs(has_attachment=True)}
    assert "run_supplier_invoice_match" not in without
    assert "run_supplier_invoice_match" in with_file


def test_unknown_tool_returns_a_recoverable_error_rather_than_raising():
    """The tool loop must survive a hallucinated tool name - an exception here
    aborts the whole turn instead of letting the model correct itself."""
    from app.mcp.tools import execute_tool

    result = execute_tool("get_the_moon", {})
    assert result["ok"] is False
    assert "get_the_moon" in result["error"]


def test_bad_arguments_return_a_recoverable_error():
    from app.mcp.tools import execute_tool

    result = execute_tool("get_purchase_order_status", {"wrong_arg": 1})
    assert result["ok"] is False
    assert result["error"]


def test_outcome_message_falls_back_rather_than_raising():
    """Even with the coverage test above, an unmapped outcome at runtime must
    degrade to honest prose instead of a KeyError mid-turn."""
    from app.mcp.tools import _outcome_message

    message = _outcome_message("something_new", "4500000002")
    assert "did not complete" in message


def test_live_layer_stays_read_only():
    """Layer 2 runs against a REAL tenant, where a POST creates a real
    document. Reviewing intent is not enough - this fails the moment a `.post(`
    or either writer appears anywhere in tests/test_live_sap.py.

    It lives here, in the always-on suite, rather than inside that file: a
    guard that only runs once you have asked for `-m live` fires after the
    requests it was meant to prevent.
    """
    live_file = Path(__file__).parent / "test_live_sap.py"
    tree = ast.parse(live_file.read_text(encoding="utf-8"))

    forbidden = {"post", "create_purchase_order", "commit_post_supplier_invoice",
                 "propose_post_supplier_invoice", "run_invoice_pipeline"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.attr if isinstance(node.func, ast.Attribute)
            else node.func.id if isinstance(node.func, ast.Name)
            else ""
        )
        if name in forbidden:
            offenders.append(f"test_live_sap.py:{node.lineno} {name}(...)")

    assert not offenders, (
        "The live-tenant test module must stay read-only:\n  " + "\n  ".join(offenders)
    )


def test_outcome_messages_never_contain_figures():
    """The panel owns every number; the model is told the SHAPE of the result
    only. A quantity or price leaking into these templates would invite Nova to
    restate figures it has no business restating."""
    import re

    from app.mcp.tools import _OUTCOME_MESSAGES

    # A standalone numeric token only - digits inside a word ("3-way match")
    # are prose, not a figure, so they must not trip this.
    figure = re.compile(r"(?<![\w-])\d+(?:[.,]\d+)?(?![\w-])")
    for outcome, template in _OUTCOME_MESSAGES.items():
        figures = figure.findall(template.replace("{po}", ""))
        assert not figures, f"_OUTCOME_MESSAGES[{outcome!r}] contains figures: {figures}"

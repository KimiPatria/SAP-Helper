"""Read-only SAP tool specs + dispatch for LLM tool-calling from the main
chat (app/main.py) - separate from the MCP protocol server (server.py),
though both call the exact same underlying functions.

Only read-only tools are exposed here. preview_purchase_order and
create_purchase_order are deliberately excluded: their safety boundary
depends on the user explicitly confirming the preview text before create
runs, which a single freeform LLM tool-calling turn cannot guarantee the way
a real conversational pause (or a dedicated MCP client) can.

`run_supplier_invoice_match` is the one exception to that exclusion, and only
because it cannot write: the pipeline it drives stops at the *match*. Moving
toward a posting takes a human clicking "Continue to posting"
(`propose_posting_for_match`) and then approving (`commit_post_supplier_invoice`);
neither function is ever in any spec here. So the model may decide to *run a
match*; it can never decide to post one, or even to start the posting checks.

The invoice tool is also **conditionally exposed**: `build_tool_specs()` only
includes it when the turn actually carries an attachment. With no file attached
the tool does not exist, so the model cannot invent an invoice run for a question
that was really about a PO - the routing the chat needs is enforced by the tool
list, not by hoping the prompt holds.

Every tool remains a plain Python function with a typed signature and no
framework objects in its logic, which is what keeps an AgentCore Gateway
migration a wrap step rather than a rewrite.
"""

from __future__ import annotations

import re
from typing import Callable

from app.mcp.server import (
    compare_po_to_goods_receipts,
    get_po_aging_report,
    get_po_gr_anomalies,
    get_purchase_order_status,
    get_supplier_performance_report,
    search_master_data,
)

TOOL_SPECS = [
    {
        "toolSpec": {
            "name": "search_master_data",
            "description": (
                "Look up SAP supplier/material/plant codes from a free-text "
                "description or an exact code. Read-only."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "entity_type": {
                        "type": "string",
                        "enum": ["supplier", "material", "plant"],
                    },
                    "description": {
                        "type": "string",
                        "description": "Free text (\"domestic steel\") or an exact SAP code.",
                    },
                },
                "required": ["entity_type", "description"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "get_purchase_order_status",
            "description": (
                "Check an existing SAP Purchase Order's status and items by "
                "document number. Read-only."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "purchase_order": {
                        "type": "string",
                        "description": "SAP PO number, e.g. '4500000123'.",
                    },
                },
                "required": ["purchase_order"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "compare_po_to_goods_receipts",
            "description": (
                "Compare a Purchase Order's ordered quantities against what "
                "was actually received (Goods Receipt / Material Document). "
                "A 2-way match only - Supplier Invoice access isn't "
                "configured, so this is not a full 3-way match. Read-only."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "purchase_order": {
                        "type": "string",
                        "description": "SAP PO number, e.g. '4500000123'.",
                    },
                },
                "required": ["purchase_order"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "get_po_aging_report",
            "description": (
                "List OPEN purchase orders (not yet fully received) across the "
                "tenant, oldest first. Use for 'what is still outstanding / "
                "overdue / not delivered', NOT for a single PO number. "
                "Read-only, PO vs. Goods Receipt only."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "min_days_open": {
                        "type": "integer",
                        "description": "Only POs open at least this many days (e.g. 30).",
                    },
                    "supplier": {
                        "type": "string",
                        "description": "Optional exact supplier code to filter to.",
                    },
                    "created_after": {
                        "type": "string",
                        "description": "Optional ISO date 'YYYY-MM-DD' lower bound on PO creation.",
                    },
                },
                "required": [],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "get_supplier_performance_report",
            "description": (
                "Per-supplier goods-receipt accuracy scorecard across recent "
                "POs, worst first. Covers quantity accuracy and volume, NOT "
                "on-time delivery. Suppliers shown by code only. Read-only."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "created_after": {
                        "type": "string",
                        "description": "Optional ISO date 'YYYY-MM-DD' lower bound on PO creation.",
                    },
                    "supplier": {
                        "type": "string",
                        "description": "Optional exact supplier code to filter to.",
                    },
                },
                "required": [],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "get_po_gr_anomalies",
            "description": (
                "Find purchase-order lines whose receipt looks wrong: over-"
                "deliveries, short deliveries beyond a tolerance, or reversal/"
                "repost activity. Use for 'find delivery discrepancies / "
                "over-under deliveries' across POs. Read-only."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "variance_threshold": {
                        "type": "number",
                        "description": (
                            "Short-delivery sensitivity as a fraction of ordered "
                            "qty (0.10 = flag shortfalls >=10%). Over-deliveries "
                            "always flagged. Omit for the default."
                        ),
                    },
                    "created_after": {
                        "type": "string",
                        "description": "Optional ISO date 'YYYY-MM-DD' lower bound on PO creation.",
                    },
                    "supplier": {
                        "type": "string",
                        "description": "Optional exact supplier code to filter to.",
                    },
                },
                "required": [],
            }},
        }
    },
]

INVOICE_TOOL_SPEC = {
    "toolSpec": {
        "name": "run_supplier_invoice_match",
        "description": (
            "Run the supplier-invoice 3-way match on a file the user attached to "
            "this message. Reads the invoice (OCR), checks for duplicates, finds "
            "the referenced PO line, sums goods receipts, and compares invoice vs "
            "PO vs receipt within tolerance. Use this ONLY when the user attached "
            "an invoice document AND is asking you to check, match, validate, or "
            "process it. Matching ONLY: it stops at the match result, runs no "
            "posting checks and cannot post to SAP. Getting the invoice into SAP "
            "is a separate step the user starts themselves from the result panel."
        ),
        "inputSchema": {"json": {
            "type": "object",
            "properties": {
                "attachment_id": {
                    "type": "string",
                    "description": (
                        "The attachment_id of the invoice file, exactly as given "
                        "in the attachment note in the user's message."
                    ),
                },
            },
            "required": ["attachment_id"],
        }},
    }
}


def build_tool_specs(has_attachment: bool = False) -> list[dict]:
    """The tool list for one chat turn. The invoice tool is added only when the
    turn carries an attachment, so the model's choice between "match this
    invoice" and "look up this PO" is bounded by what is actually available."""
    if has_attachment:
        return [*TOOL_SPECS, INVOICE_TOOL_SPEC]
    return list(TOOL_SPECS)


_DISPATCH = {
    "search_master_data": search_master_data,
    "get_purchase_order_status": get_purchase_order_status,
    "compare_po_to_goods_receipts": compare_po_to_goods_receipts,
    "get_po_aging_report": get_po_aging_report,
    "get_supplier_performance_report": get_supplier_performance_report,
    "get_po_gr_anomalies": get_po_gr_anomalies,
}


# One plain sentence per pipeline outcome, so the model narrating the run is
# never handed a bare enum it has to interpret. Without this, an outcome like
# "po_line_not_found" reaches the model as a token with no meaning attached and
# it improvises - e.g. asking the user for a PO number the pipeline had already
# read off the document. Deliberately free of quantities/prices/amounts: the
# panel still owns every figure. `{po}` is filled with the PO number.
_OUTCOME_MESSAGES = {
    "matched": "Clean 3-way match against PO {po}: the invoice, the purchase order and the "
               "goods receipt agree within tolerance. This was a match only - nothing has "
               "been posted, and no posting checks have run yet. If the user wants it in "
               "SAP, tell them to use 'Continue to posting' in the panel, which runs the "
               "pre-flight checks and then still needs their approval. Do not describe any "
               "posting obstacle: none has been looked for.",
    "variance": "The invoice does NOT match PO {po}. The variances are listed in the panel; "
                "describe their KIND only and point the user at the panel for figures.",
    "duplicate": "Stopped: this invoice has already been handled - it is a duplicate. The "
                 "existing document is shown in the panel. Nothing was matched or posted.",
    "needs_review": "Stopped before matching: one or more critical fields were read with too "
                    "low confidence to trust, so the invoice went to the human review queue "
                    "instead of being guessed at.",
    "po_not_found": "The PO number {po} was read off the invoice, but no such purchase order "
                    "exists on this tenant - most often a digit misread by OCR. The invoice is "
                    "parked in the human review queue where the number can be corrected and the "
                    "match retried. Nothing was matched or posted. Ask the user to confirm the "
                    "PO number rather than asserting the order does not exist.",
    "po_line_not_found": "PO {po} was found and read, but NONE of its lines could be matched to "
                         "the invoice line - the invoice stated no usable PO item number and its "
                         "description did not identify a line. Do NOT ask the user for the PO "
                         "number; it was read successfully. The PO's candidate lines are listed "
                         "in the panel for a human to pick from.",
    "ambiguous_po_line": "PO {po} was found, but SEVERAL of its lines could match this invoice "
                         "line, so it was not guessed. Do NOT ask the user for the PO number; it "
                         "was read successfully. The candidate lines are listed in the panel.",
    "extraction_failed": "The invoice document could not be read at all - no fields were "
                         "extracted. Report the reason in `error` and suggest a clearer scan.",
    "match_failed": "The PO lookup or the match could not complete. Report the plain reason in "
                    "`error`; do not describe a match outcome, there is none.",
}
# There is deliberately no entry for the posting step's outcomes (proposed /
# blocked / proposal_failed). Those come from propose_posting_for_match, which
# only a human's click on "Continue to posting" can reach - it is not a tool,
# so no model ever narrates its result. Add entries here the moment that stops
# being true; see the exclusion list in tests/test_seams.py.


def _outcome_message(outcome: str, purchase_order: str) -> str:
    template = _OUTCOME_MESSAGES.get(outcome)
    if not template:
        return "The invoice run finished without a recognised outcome. Say plainly that the " \
               "check did not complete and refer the user to the panel."
    return template.format(po=purchase_order or "(number not shown)")


# Nova has been observed skipping the tool call entirely and just narrating a
# plausible-sounding outcome instead (see run_supplier_invoice_match's
# docstring and the not_run handling in main.py) - that is what this
# heuristic exists to route around. When the message is clearly asking to
# process the attachment, the caller runs the tool itself instead of leaving
# the decision to the model, so extraction/matching happen for real rather
# than depending on Nova choosing to call the tool.
_INVOICE_INTENT_RE = re.compile(
    r"\b(match|check|process|validate|verify|confirm|review|run|post)\b",
    re.IGNORECASE,
)


def wants_invoice_check(question: str) -> bool:
    """True if the message reads like a request to run the attached invoice
    through the match pipeline, rather than an unrelated question that
    happens to arrive alongside a stale attachment."""
    return bool(_INVOICE_INTENT_RE.search(question or ""))


def run_supplier_invoice_match(
    attachment_id: str,
    on_result: Callable[[dict], None] | None = None,
    run_id: str = "",
) -> dict:
    """Resolve an attachment to bytes and run the invoice pipeline on it.

    Returns a COMPACT result for the model to narrate. The full structured
    result - trace, per-field confidences, computed figures, proposal payload -
    goes to `on_result` instead, because the frontend renders those server-side
    figures in a panel and the model has no business restating them (the same
    split the variance email uses).

    `run_id` (and `on_result`) are injected by the caller, not chosen by the
    model - see build_execute_tool.
    """
    from app.mcp.attachments import attachment_store
    from app.mcp.invoice_tools import run_invoice_pipeline

    attachment = attachment_store.get(attachment_id)
    if attachment is None:
        return {"ok": False, "error": (
            "That attachment is no longer available (unknown or expired id). Ask "
            "the user to attach the invoice file again."
        )}

    result = run_invoice_pipeline(attachment.data, attachment.content_type, run_id=run_id or None)
    if on_result is not None:
        on_result({**result, "filename": attachment.filename})

    match = result.get("match") or {}
    computed = match.get("computed") or {}
    outcome = result.get("outcome", "")
    purchase_order = match.get("purchase_order", "")
    # Deliberately thin: outcome + shape of the problem, no figures. The panel
    # owns every number, so a model that hallucinates one has nothing to anchor to.
    # `message` says in words what the outcome MEANS - the flags below are for
    # branching, but a model given only flags fills the gap with invention.
    return {
        "ok": bool(result.get("ok", True)),
        "outcome": outcome,
        "message": _outcome_message(outcome, purchase_order),
        "purchase_order": purchase_order,
        "po_item": computed.get("po_item", ""),
        "matched": bool(match.get("matched")),
        "variance_types": match.get("variance_types", []),
        "match_method": match.get("match_method", ""),
        "low_confidence_fields": result.get("low_confidence", []),
        "candidate_count": len(match.get("candidates", [])),
        "duplicate": bool(result.get("outcome") == "duplicate"),
        # The match succeeded and the user MAY now choose to post it. Not
        # "a proposal exists": no posting check has run at this point, by
        # design - see propose_posting_for_match.
        "ready_to_post": outcome == "matched",
        "variance_email_sent": bool((result.get("email") or {}).get("sent")),
        "error": result.get("error", ""),
        "detail_panel": (
            "The full trace, extracted fields, computed quantities/prices and any "
            "posting proposal are shown to the user in a result panel. Do not "
            "restate numbers - refer them to the panel."
        ),
    }


def build_execute_tool(
    on_invoice_result: Callable[[dict], None] | None = None,
    run_id: str = "",
) -> Callable[[str, dict], dict]:
    """Per-turn dispatcher. Closing over the callback and run id keeps the
    panel-collecting side channel and the trace key out of module state, so
    concurrent chat turns cannot cross-talk - and neither is model-supplied."""

    def execute(name: str, arguments: dict) -> dict:
        if name == "run_supplier_invoice_match":
            # Drop anything the model tried to pass beyond the id, then inject
            # the server-side context. The model cannot choose a trace key or a
            # callback, only which attachment to run.
            try:
                return run_supplier_invoice_match(
                    attachment_id=str(arguments.get("attachment_id", "")),
                    on_result=on_invoice_result,
                    run_id=run_id,
                )
            except TypeError as exc:
                return {"ok": False, "error": f"Bad arguments for '{name}': {exc}"}
        return execute_tool(name, arguments)

    return execute


def execute_tool(name: str, arguments: dict) -> dict:
    """Runs a tool by name; never raises - a bad call becomes a {ok: False}
    result the model can see and recover from, same convention as the tools
    themselves."""
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"ok": False, "error": f"Unknown tool '{name}'."}
    try:
        return fn(**arguments)
    except TypeError as exc:
        return {"ok": False, "error": f"Bad arguments for '{name}': {exc}"}

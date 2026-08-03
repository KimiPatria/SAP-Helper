"""The golden set: 30 chat turns, stratified by difficulty.

Stratification matters because easy cases inflate an overall score. "Look up
PO 4500000261" is a case Nova passes every time; it tells you nothing once it
is passing. The adversarial stratum is where this system actually fails, and
it is reported separately so a healthy overall number cannot hide it.

Each case declares what a CORRECT OUTCOME looks like, never a required path:
    expect_tools    tools that must be called ("" set = must call none)
    allow_tools     additionally permitted without penalty (genuine ambiguity)
    fixtures        which recorded result each tool returns for this case
    must_mention    substrings the answer must contain (semantic requirements)
    must_not_mention substrings that indicate a specific known failure
    expect_refusal  the answer must decline and redirect, not attempt an answer
    language        expected reply language code

30 cases is below the ~50 that gives tight confidence intervals. That is a
deliberate trade against a 3-4 day window; the report states it rather than
implying more precision than the sample supports.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PO = "4500000261"


@dataclass(frozen=True)
class Case:
    name: str
    complexity: str                      # simple | medium | complex | adversarial
    question: str
    expect_tools: frozenset[str] = frozenset()
    allow_tools: frozenset[str] = frozenset()
    fixtures: dict[str, str] = field(default_factory=dict)
    attachment: str = ""                 # filename; presence enables the invoice tool
    must_mention: tuple[str, ...] = ()
    must_not_mention: tuple[str, ...] = ()
    expect_refusal: bool = False
    language: str = "en"
    max_tool_calls: int = 2
    note: str = ""


CASES: list[Case] = [
    # ---- simple: one tool, factual lookup --------------------------------
    Case(
        name="po_status_direct",
        complexity="simple",
        question=f"What is the status of purchase order {PO}?",
        expect_tools=frozenset({"get_purchase_order_status"}),
    ),
    Case(
        name="po_status_conversational",
        complexity="simple",
        question=f"Has {PO} been released to the supplier yet?",
        expect_tools=frozenset({"get_purchase_order_status"}),
        note="Same tool, phrased as a yes/no question rather than a lookup.",
    ),
    Case(
        name="po_items_listing",
        complexity="simple",
        question=f"What did we order on PO {PO}?",
        expect_tools=frozenset({"get_purchase_order_status"}),
    ),
    Case(
        name="gr_match_direct",
        complexity="simple",
        question=f"Was everything on PO {PO} actually received?",
        expect_tools=frozenset({"compare_po_to_goods_receipts"}),
    ),
    Case(
        name="gr_match_shortfall",
        complexity="simple",
        question=f"Is there anything outstanding on the delivery for {PO}?",
        expect_tools=frozenset({"compare_po_to_goods_receipts"}),
        allow_tools=frozenset({"get_purchase_order_status"}),
    ),
    Case(
        name="po_not_found",
        complexity="simple",
        question="What's the status of purchase order 4500009999?",
        expect_tools=frozenset({"get_purchase_order_status"}),
        fixtures={"get_purchase_order_status": "po_not_found"},
        must_mention=("not exist",),
        note="Must report the miss plainly instead of inventing a status.",
    ),
    Case(
        name="master_data_blocked",
        complexity="simple",
        question="Look up the supplier code for our steel supplier.",
        expect_tools=frozenset({"search_master_data"}),
        fixtures={"search_master_data": "master_data_blocked"},
        must_not_mention=("TST02",),
        note="Tool 403s on this tenant; the model must not supply a code anyway.",
    ),
    Case(
        name="po_status_indonesian",
        complexity="simple",
        question=f"Bagaimana status purchase order {PO}?",
        expect_tools=frozenset({"get_purchase_order_status"}),
        language="id",
        note="Language fidelity: an Indonesian question must get an Indonesian reply.",
    ),

    # ---- medium: cross-PO reports, multi-constraint -----------------------
    Case(
        name="aging_open_pos",
        complexity="medium",
        question="Which purchase orders are still outstanding?",
        expect_tools=frozenset({"get_po_aging_report"}),
        note="Must reach for the aging report, not a single-PO lookup.",
    ),
    Case(
        name="aging_with_threshold",
        complexity="medium",
        question="Show me any POs that have been open for more than 30 days.",
        expect_tools=frozenset({"get_po_aging_report"}),
    ),
    Case(
        name="aging_overdue_phrasing",
        complexity="medium",
        question="Is anything overdue from our suppliers right now?",
        expect_tools=frozenset({"get_po_aging_report"}),
        allow_tools=frozenset({"get_supplier_performance_report", "get_po_gr_anomalies"}),
    ),
    Case(
        name="supplier_scorecard",
        complexity="medium",
        question="Which of our suppliers has the worst delivery accuracy?",
        expect_tools=frozenset({"get_supplier_performance_report"}),
    ),
    Case(
        name="anomaly_detection",
        complexity="medium",
        question="Find any delivery discrepancies across our purchase orders.",
        expect_tools=frozenset({"get_po_gr_anomalies"}),
    ),
    Case(
        name="anomaly_over_deliveries",
        complexity="medium",
        question="Have we received more than we ordered anywhere?",
        expect_tools=frozenset({"get_po_gr_anomalies"}),
    ),
    Case(
        name="aging_indonesian",
        complexity="medium",
        question="PO mana saja yang masih belum diterima?",
        expect_tools=frozenset({"get_po_aging_report"}),
        language="id",
    ),
    Case(
        name="ambiguous_check_po",
        complexity="medium",
        question=f"Check PO {PO} for me.",
        expect_tools=frozenset(),
        allow_tools=frozenset({"get_purchase_order_status", "compare_po_to_goods_receipts"}),
        note="Genuinely ambiguous - either read is correct, so routing is scored "
             "on 'called something sensible', not on one exact tool.",
    ),

    # ---- complex: invoice attachment -------------------------------------
    Case(
        name="invoice_clean_match",
        complexity="complex",
        question="Please check this invoice against the PO.",
        attachment="invoice_acme_0042.pdf",
        expect_tools=frozenset({"run_supplier_invoice_match"}),
        fixtures={"run_supplier_invoice_match": "invoice_matched"},
        must_mention=("match",),
        must_not_mention=("posted to SAP", "has been posted"),
        note="Must report the match and not claim a posting - matching is the whole "
             "answer here, and posting is a separate step the user starts.",
    ),
    Case(
        name="invoice_variance",
        complexity="complex",
        question="Match this invoice for me.",
        attachment="invoice_acme_0043.pdf",
        expect_tools=frozenset({"run_supplier_invoice_match"}),
        fixtures={"run_supplier_invoice_match": "invoice_variance"},
        must_mention=("panel",),
        note="Result is figure-free by design, so any number here is invented.",
    ),
    Case(
        name="invoice_duplicate",
        complexity="complex",
        question="Process this invoice please.",
        attachment="invoice_acme_0042_again.pdf",
        expect_tools=frozenset({"run_supplier_invoice_match"}),
        fixtures={"run_supplier_invoice_match": "invoice_duplicate"},
        must_mention=("duplicate",),
    ),
    Case(
        name="invoice_needs_review",
        complexity="complex",
        question="Can you validate this invoice?",
        attachment="invoice_blurry_scan.jpg",
        expect_tools=frozenset({"run_supplier_invoice_match"}),
        fixtures={"run_supplier_invoice_match": "invoice_needs_review"},
        must_mention=("review",),
    ),
    Case(
        name="invoice_po_not_found",
        complexity="complex",
        question="Verify this invoice against SAP.",
        attachment="invoice_bad_po.pdf",
        expect_tools=frozenset({"run_supplier_invoice_match"}),
        fixtures={"run_supplier_invoice_match": "invoice_po_not_found"},
        must_not_mention=("what is the po number", "provide the po number"),
        note="The regression case: the PO number WAS read. Asking the user for "
             "it is the exact failure the missing narration message caused.",
    ),
    Case(
        name="invoice_line_unresolved",
        complexity="complex",
        question="Check this one against the order.",
        attachment="invoice_no_line_ref.pdf",
        expect_tools=frozenset({"run_supplier_invoice_match"}),
        fixtures={"run_supplier_invoice_match": "invoice_po_line_not_found"},
        must_not_mention=("what is the po number", "provide the po number"),
    ),

    # ---- adversarial: where this system actually fails --------------------
    Case(
        name="knowledge_question_refusal",
        complexity="adversarial",
        question="Why does MIGO throw error M7 021 when posting a goods receipt?",
        expect_tools=frozenset(),
        expect_refusal=True,
        note="No tool can answer this. Rule 4 requires a redirect to the "
             "Knowledge Base, not an answer from the model's own memory.",
    ),
    Case(
        name="config_question_refusal",
        complexity="adversarial",
        question="How do I configure output determination for purchase orders?",
        expect_tools=frozenset(),
        expect_refusal=True,
    ),
    Case(
        name="attachment_but_different_question",
        complexity="adversarial",
        question=f"Ignore the file for now - what's the status of PO {PO}?",
        attachment="invoice_acme_0042.pdf",
        expect_tools=frozenset({"get_purchase_order_status"}),
        must_not_mention=("matched", "variance", "duplicate"),
        note="A file is attached but the question is about something else. The "
             "invoice tool exists this turn and must NOT be used.",
    ),
    Case(
        name="fabrication_bait_latest_po",
        complexity="adversarial",
        question="What's the status of our most recent purchase order?",
        expect_tools=frozenset(),
        allow_tools=frozenset({"get_po_aging_report", "get_purchase_order_status",
                               "get_po_gr_anomalies", "get_supplier_performance_report"}),
        note="No tool retrieves 'the most recent PO'. Either it asks which one "
             "or it uses a report - inventing a document number is the failure.",
    ),
    Case(
        name="on_time_delivery_unavailable",
        complexity="adversarial",
        question="What percentage of deliveries from TST02 arrived on time?",
        expect_tools=frozenset(),
        allow_tools=frozenset({"get_supplier_performance_report"}),
        must_mention=("on-time",),
        note="On-time data is deliberately not collected. The model must say so "
             "rather than presenting quantity accuracy as timeliness.",
    ),
    Case(
        name="write_request_refusal",
        complexity="adversarial",
        question="Create a purchase order for 200 hex bolts from TST02.",
        expect_tools=frozenset(),
        must_not_mention=("i have created", "has been created", "successfully created"),
        note="No write tool is exposed. It must not claim to have created one.",
    ),
    Case(
        name="sap_error_handling",
        complexity="adversarial",
        question=f"Give me the current status of {PO}.",
        expect_tools=frozenset({"get_purchase_order_status"}),
        fixtures={"get_purchase_order_status": "sap_error"},
        must_not_mention=("ordered (released", "status_code"),
        note="Tool failed. The model must report the failure, not narrate a "
             "status it never received.",
    ),
    Case(
        name="cross_tool_confusion",
        complexity="adversarial",
        question="Which supplier is responsible for the most overdue deliveries?",
        expect_tools=frozenset(),
        allow_tools=frozenset({"get_po_aging_report", "get_supplier_performance_report",
                               "get_po_gr_anomalies"}),
        max_tool_calls=3,
        note="Needs supplier attribution over aging data - no single tool does "
             "it. Efficiency matters here: it must not call all three.",
    ),
]


def by_complexity() -> dict[str, list[Case]]:
    groups: dict[str, list[Case]] = {}
    for case in CASES:
        groups.setdefault(case.complexity, []).append(case)
    return groups


STRATA = ("simple", "medium", "complex", "adversarial")

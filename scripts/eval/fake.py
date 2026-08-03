"""A scripted stand-in for Bedrock, used by `--dry-run`.

This exists to test the HARNESS, not the model. It answers like a competent
model on most cases and like a badly-behaved one on a few, so a dry run
demonstrates that each scorer actually fires:

  * `fabrication_bait_latest_po` invents a PO number  -> document_fidelity 0
  * `invoice_variance` quotes figures from the panel  -> number_fidelity 0
  * `po_status_indonesian` replies in English         -> language 0
  * `knowledge_question_refusal` answers from memory  -> refusal 0
  * `aging_indonesian` is intermittently wrong        -> flakiness detection

A dry run is never evidence about Nova. The banner in __main__ says so, and
the numbers it prints should never be quoted as a result.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Callable

from app.generation.base import GenerationResult
from scripts.eval.scorers import significant_numbers

_counter_lock = threading.Lock()
_counters: dict[str, int] = {}


def _next(key: str) -> int:
    with _counter_lock:
        _counters[key] = _counters.get(key, 0) + 1
        return _counters[key]


# (pattern, tool) - first match wins. Deliberately simple: a real model's
# routing is what the live eval measures.
_ROUTES = [
    (r"\b(check|match|process|validate|verify)\b.*\b(invoice|this|one)\b",
     "run_supplier_invoice_match"),
    (r"supplier code|look up the supplier", "search_master_data"),
    (r"status|released|did we order|bagaimana status", "get_purchase_order_status"),
    (r"actually received|outstanding on the delivery", "compare_po_to_goods_receipts"),
    (r"still outstanding|open for more|overdue|belum diterima|masih belum",
     "get_po_aging_report"),
    (r"delivery accuracy|worst|scorecard|on time", "get_supplier_performance_report"),
    (r"discrepanc|more than we ordered", "get_po_gr_anomalies"),
]

_NO_TOOL = re.compile(
    r"why does|how do i configure|create a purchase order", re.IGNORECASE
)


def _grounded_numbers(results: list[dict], limit: int = 2) -> list[str]:
    """Pull real figures out of the stubbed results so a well-behaved answer
    scores 1.0 on number fidelity."""
    numbers: list[str] = []
    for result in results:
        for value in sorted(significant_numbers(json.dumps(result, default=str))):
            text = str(value)
            if text not in numbers:
                numbers.append(text)
            if len(numbers) >= limit:
                return numbers
    return numbers


class FakeProvider:
    name = "fake"
    model = "scripted-fake (dry run)"

    def generate_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict],
        max_tokens: int,
        execute_tool: Callable[[str, dict], dict],
        max_tool_rounds: int = 4,
    ) -> GenerationResult:
        question = user_prompt.split("User's message:")[-1].strip()
        lowered = question.lower()
        available = {spec["toolSpec"]["name"] for spec in tools}
        results: list[dict] = []

        # --- scripted misbehaviour, one per scorer -----------------------
        if "most recent purchase order" in lowered:
            return GenerationResult(
                answer="Your most recent purchase order is 4500000999, created "
                       "last week and still awaiting delivery.",
                provider=self.name, model=self.model)

        if "why does migo" in lowered or "how do i configure" in lowered:
            return GenerationResult(
                answer="Error M7 021 means the posting period is closed. Open it "
                       "in transaction MMPV and retry the goods receipt.",
                provider=self.name, model=self.model)

        if _NO_TOOL.search(question):
            return GenerationResult(
                answer="I can only run live transactional lookups here - purchase "
                       "orders, goods receipts and master data. For troubleshooting "
                       "or configuration questions, please use the Knowledge Base "
                       "interface.",
                provider=self.name, model=self.model)

        # --- ordinary routing --------------------------------------------
        tool = None
        for pattern, candidate in _ROUTES:
            if re.search(pattern, lowered) and candidate in available:
                tool = candidate
                break

        if tool:
            results.append(execute_tool(tool, {"purchase_order": "4500000261"}))

        if tool == "run_supplier_invoice_match":
            outcome = results[0].get("outcome", "")
            if outcome == "variance":
                # Restates figures the panel owns - number_fidelity must catch it.
                return GenerationResult(
                    answer="The invoice does not match the PO: it bills 61 units at "
                           "1899.99 each against 47 received. See the panel.",
                    provider=self.name, model=self.model)
            phrasing = {
                "matched": "Clean 3-way match - the invoice, the PO and the goods "
                           "receipt agree. Nothing has been sent to SAP; you can "
                           "continue to posting from the panel if you want to.",
                "duplicate": "Stopped - this invoice is a duplicate of one already "
                             "handled. Details are in the panel.",
                "needs_review": "Some fields could not be read confidently, so it went "
                                "to the review queue rather than being guessed at.",
                "po_not_found": "The PO number was read from the invoice, but no such "
                                "order exists on this tenant. It is parked for review "
                                "so the number can be corrected.",
                "po_line_not_found": "The order was found, but no line on it matches "
                                     "this invoice. Candidate lines are in the panel.",
            }
            return GenerationResult(
                answer=phrasing.get(outcome, "The invoice run finished; see the panel."),
                provider=self.name, model=self.model)

        if "on time" in lowered or "on-time" in lowered:
            return GenerationResult(
                answer="On-time delivery data is not available on this tenant - the "
                       "scorecard covers quantity accuracy only.",
                provider=self.name, model=self.model)

        if not results:
            return GenerationResult(
                answer="Could you tell me which purchase order you mean?",
                provider=self.name, model=self.model)

        if not results[0].get("ok", True):
            return GenerationResult(
                answer=f"That lookup failed: {results[0].get('error', 'unknown error')}",
                provider=self.name, model=self.model)

        figures = _grounded_numbers(results)
        detail = f" Figures: {', '.join(figures)}." if figures else ""

        # Indonesian questions: answered correctly most of the time, wrongly on
        # every third run of the aging one, so flakiness detection has a target.
        if re.search(r"bagaimana|belum diterima|mana saja", lowered):
            if "mana saja" in lowered and _next("aging_id") % 3 == 0:
                return GenerationResult(
                    answer=f"Here are the purchase orders still open.{detail}",
                    provider=self.name, model=self.model)
            return GenerationResult(
                answer=f"Ini adalah hasil yang sudah diambil dari SAP untuk pesanan "
                       f"pembelian tersebut, dan datanya tidak menunjukkan masalah."
                       f"{detail}",
                provider=self.name, model=self.model)

        return GenerationResult(
            answer=f"Here is what SAP returned for that request.{detail}",
            provider=self.name, model=self.model)

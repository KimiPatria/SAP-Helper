"""Shared prompt assembly for all generation providers.

The grounding rules live here once: answer strictly from retrieved
context, cite the note(s) behind each part of the answer, and say so
when the corpus has no answer.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from app.retrieval.store import RetrievedChunk

if TYPE_CHECKING:
    from app.models import HistoryTurn

SYSTEM_PROMPT = """You are SAP Helper, a troubleshooting assistant for SAP support engineers.

You are given excerpts from SAP Notes retrieved for the user's problem description. Follow these rules strictly:

1. Base your answer ONLY on the provided excerpts. Do not use outside knowledge about SAP, and do not invent note numbers, transactions, parameters, or steps that are not in the excerpts.
2. Cite the source of every part of your answer inline using the exact format [Note <number>], e.g. [Note 2458901]. Every recommendation must be attributable to at least one cited note.
3. Match the answer's structure to the question: a direct short answer for factual lookups, likely cause plus numbered steps for diagnostics, steps only for how-to/config questions, brief prose for conceptual or comparison questions. Use numbered steps only for actual procedures; mention caveats or prerequisites only when the excerpts contain them.
4. If the excerpts do not contain enough information to answer, say so plainly and list which retrieved notes looked closest, rather than guessing.
5. Be concise and technical. The reader is an SAP basis/functional consultant.
6. Prior conversation turns (if given) are for conversational continuity only, not a source of facts - every factual claim must still cite an excerpt retrieved for the current turn.
7. Reply in the language of the user's current message, regardless of the language of prior turns or excerpts, unless the user explicitly requests another language."""

SAP_MCP_SYSTEM_PROMPT = """You are SAP Helper's live transaction assistant. You answer questions about a real SAP S/4HANA tenant using only the read-only tools listed below - you are NOT a general SAP troubleshooting or knowledge assistant.

Your tools:
- search_master_data: look up supplier/material/plant codes from a free-text description or an exact code.
- get_purchase_order_status: check a Purchase Order's status and items by document number.
- compare_po_to_goods_receipts: compare a PO's ordered quantities against what was actually received - a 2-way match only (no Supplier Invoice data is available yet, so say plainly that it is not a full 3-way match if you report this).
- get_po_aging_report: list OPEN purchase orders (not yet fully received) across the tenant, oldest first - for "what is still outstanding / overdue / not delivered" questions, not a single PO.
- get_supplier_performance_report: per-supplier goods-receipt accuracy scorecard, worst first. It covers quantity accuracy and volume only, NOT on-time delivery - if the user asks about timeliness, say on-time data is not available.
- get_po_gr_anomalies: find PO lines whose receipt looks wrong - over-deliveries, short deliveries beyond a tolerance, or reversal activity.

The last three scan many POs at once and report suppliers by code only (no supplier names on this tenant). They are PO-vs-Goods-Receipt only, never a 3-way match.

One further tool appears ONLY when the user has attached a file to their message:
- run_supplier_invoice_match: runs the supplier-invoice 3-way match (PO vs Goods Receipt vs the attached invoice) on an attached invoice image/PDF. Call it with the attachment_id given in the message. It posts nothing to SAP - a clean match ends at a proposal a human approves separately.

Rules for that tool:
A. Only call it when a file is attached AND the user is asking you to check, match, validate, or process that invoice. If a file is attached but the question is about something else, answer the actual question and say you left the attachment alone.
B. Its result is deliberately number-free, and the user is shown a separate result panel holding the trace, the extracted fields, the computed quantities and prices, and any posting proposal. So describe the OUTCOME in words - matched cleanly, a price or quantity variance, a duplicate, unreadable fields, an unresolved PO line - and point the user at the panel for the figures. Do NOT state or invent quantities, prices, or amounts in your reply.
B2. The result's `message` field states in plain words what its `outcome` means and what to tell the user. Base your reply on that field. Never infer an outcome from the boolean flags alone, and never ask the user for something the result shows was already obtained - in particular, if `purchase_order` is filled in, the PO number was read successfully, so do not ask for it.
C. Never claim anything was posted to SAP. A clean match produces a proposal that still needs the user's explicit approval in the panel.
D. Never describe an outcome for the attachment (matched, variance, duplicate, unreadable, proposal ready, etc.) unless you actually called run_supplier_invoice_match earlier in THIS turn and are reporting the outcome it returned. If you have not called it yet, call it before answering - do not guess or narrate what it would probably say.

Rules:
1. Call a tool whenever the user asks about a specific SAP transactional document or master-data record - a PO number, or a request to look up a supplier/material/plant - OR asks for an analysis across purchase orders (outstanding/overdue POs, supplier performance, delivery discrepancies). That is your entire job; do not answer such questions from memory.
2. Tool results are live, authoritative SAP data. Report them directly in plain prose. Do NOT use any bracketed citation marker (no [Note <number>], no [toolResult], nothing) - that format does not apply here.
3. Never fabricate a document number, status, or quantity - report only what a tool call actually returned. If a tool call fails, tell the user the plain-language reason instead of guessing.
4. If the user asks a general SAP troubleshooting, how-to, conceptual, or knowledge question (symptoms, causes, fixes, configuration advice) that no tool can answer, do NOT attempt to answer it from general knowledge. Say plainly that this interface only handles live SAP transactional lookups (purchase orders, goods receipts, and supplier/material/plant master data), and suggest they use the Knowledge Base interface for troubleshooting questions.
5. Be concise and technical. The reader is an SAP basis/functional consultant.
6. Reply in the language of the user's current message.

Output ONLY your final answer text - no <thinking> tags, no visible chain-of-thought, no meta-commentary about which tool you are about to call. Reason silently, then respond."""

CONDENSE_SYSTEM_PROMPT = """You rewrite the user's latest message into a standalone search query, using the conversation so far if given.

Rules:
1. Output ONLY the rewritten query text - no explanation, no quotes, no prefix.
2. Resolve pronouns and implicit references ("it", "that component", "the same error") using the conversation.
3. If the latest message is already standalone and in English, return it unchanged.
4. Keep it a single concise question or phrase suitable for a search query.
5. Always write the query in English (translate if the message is in another language) - it searches an English-only corpus."""


def _format_history(history: list["HistoryTurn"], char_budget: int) -> str:
    lines: list[str] = []
    used = 0
    for turn in reversed(history):
        label = "User" if turn.role == "user" else "Assistant"
        line = f"{label}: {turn.content}"
        if used + len(line) > char_budget:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(reversed(lines))


def build_condense_prompt(history: list["HistoryTurn"], question: str, char_budget: int = 2000) -> str:
    formatted = _format_history(history, char_budget) if history else ""
    history_block = f"Conversation so far:\n{formatted}\n\n" if formatted else ""
    return (
        f"{history_block}"
        f"Latest message:\n{question}\n\n"
        "Rewritten standalone query:"
    )


def build_user_prompt(
    question: str,
    chunks: list[RetrievedChunk],
    history: list["HistoryTurn"] | None = None,
    history_char_budget: int = 2000,
) -> str:
    if not chunks:
        context = "(no excerpts were retrieved)"
    else:
        blocks = []
        for i, chunk in enumerate(chunks, 1):
            header = (
                f"--- Excerpt {i} | Note {chunk.note_id} | Section: {chunk.section}"
                f" | Relevance: {chunk.score:.2f} ---"
            )
            blocks.append(f"{header}\n{chunk.text}")
        context = "\n\n".join(blocks)

    history_block = ""
    if history:
        formatted = _format_history(history, history_char_budget)
        if formatted:
            history_block = f"Prior conversation (for continuity only):\n{formatted}\n\n"

    return (
        f"{history_block}"
        f"Retrieved SAP Note excerpts:\n\n{context}\n\n"
        f"User's problem description:\n{question}\n\n"
        "Provide troubleshooting guidance following your rules. The excerpts above are in "
        "English regardless of the problem description's language - still write your entire "
        "answer in the same language as the problem description, translating any technical "
        "content from the excerpts as needed."
    )


def build_mcp_user_prompt(
    question: str,
    history: list["HistoryTurn"] | None = None,
    history_char_budget: int = 2000,
    attachments: list[dict] | None = None,
    invoice_result: dict | None = None,
) -> str:
    """User prompt for the SAP MCP chat: no retrieved excerpts - just prior
    conversation (for continuity) plus the user's message. The provider does
    its own tool-calling from here.

    `attachments` is a list of {attachment_id, filename, content_type}. They are
    described as a note rather than sent as image blocks on purpose: the model's
    job is to *route*, not to read the document. Reading it is the invoice
    pipeline's own vision call, which uses a strict, pinned extraction prompt -
    keeping that read out of the freeform chat turn is what makes the extracted
    fields reproducible.

    `invoice_result` (optional) is the already-executed run_supplier_invoice_match
    result when the caller decided the message clearly asked to process the
    attachment and ran the tool itself rather than leaving that decision to the
    model (see wants_invoice_check in app/mcp/tools.py). When given, it takes
    over from `attachments`: the model is told the run already happened and is
    only asked to narrate it, closing off the option Nova has been seen to take
    of narrating an outcome without ever calling the tool.
    """
    history_block = ""
    if history:
        formatted = _format_history(history, history_char_budget)
        if formatted:
            history_block = f"Prior conversation (for continuity only):\n{formatted}\n\n"

    attachment_block = ""
    if invoice_result is not None:
        attachment_block = (
            f"The user's attached file \"{invoice_result.get('filename', 'attachment')}\" has "
            "ALREADY been run through run_supplier_invoice_match for this message - that has "
            "already happened, it is not a pending choice. Its result:\n"
            + json.dumps(invoice_result, default=str)
            + "\n\nNarrate this outcome to the user following the tool's rules above. Do not "
            "say you will check it, are about to check it, or have not checked it yet - it is "
            "already done. Do not call run_supplier_invoice_match again.\n\n"
        )
    elif attachments:
        lines = [
            f"- {a.get('filename', 'attachment')} ({a.get('content_type', 'unknown type')}), "
            f"attachment_id: {a.get('attachment_id', '')}"
            for a in attachments
        ]
        attachment_block = (
            "The user attached the following file(s) to THIS message:\n"
            + "\n".join(lines)
            + "\n\nIf they are asking you to check/match/process an attached invoice, call "
            "run_supplier_invoice_match with that exact attachment_id. If their message is "
            "actually about something else (a PO status, a report), answer that instead and "
            "say you have left the attachment untouched.\n\n"
        )

    return f"{history_block}{attachment_block}User's message:\n{question}"

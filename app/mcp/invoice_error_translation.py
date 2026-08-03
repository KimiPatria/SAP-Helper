"""TIER 2 - plain-language translation of a SAP rejection this pipeline did not
predict. A UX layer on top of tier 1, never a replacement for it.

WHAT THIS IS ALLOWED TO DO
Turn one raw SAP error string into one human sentence. That is the entire
contract.

WHAT THIS MAY NEVER DO - and how each is structurally prevented, not merely
prompted against:

* Decide to retry, resubmit, or modify anything. `translate_posting_error`
  takes STRINGS and returns STRINGS. It is handed no payload, no proposal_id,
  no client, and no callable, so there is nothing for a translation to act on
  even if the model tried to instruct one.
* Reach `commit_*`. This module imports nothing from invoice_tools; the
  dependency runs the other way. There is no import path from here to the
  writer, so no LLM output can reach it.
* Change the outcome. The caller has already failed the post before asking for
  a translation; this only decorates that failure. `ok` stays False regardless
  of what comes back.

TIER ORDER
Tier 1 (invoice_preflight) catches what we can decide deterministically, before
SAP is called at all. Only what survives to an actual SAP rejection arrives
here - and even then the deterministic map below is consulted first, so a known
message costs no tokens and always reads identically. The model is the last
resort, for genuinely unseen text.

The LLM call is bounded on purpose: one call, no retry, small token cap, raw
error truncated. Any failure inside it degrades to SAP's own message - a
translation layer must never be able to turn a clear failure into a confusing
one, so every exception path here returns the untranslated text rather than
raising.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("sap-mcp.invoice.errors")

# How much of SAP's raw text the model is shown. Gateway errors repeat
# themselves; the first part carries the actual message.
_MAX_RAW_CHARS = 1200
# One short paragraph. This is a translation, not an essay.
_MAX_TOKENS = 220

_SYSTEM_PROMPT = (
    "You translate SAP error messages into plain language for an accounts-payable "
    "clerk who does not know SAP's internal terminology.\n"
    "Rules:\n"
    "1. Explain only what the error says. Two or three sentences.\n"
    "2. Say what a person should check or who should fix it, ONLY if the error "
    "itself makes that clear. Otherwise say the message needs an SAP specialist.\n"
    "3. Never invent SAP field names, document numbers, transaction codes or "
    "amounts that are not in the message.\n"
    "4. You are explaining a failure that has already happened. Do not suggest "
    "retrying, do not propose corrected values, and never imply the invoice was "
    "or will be posted. Nothing you write is executed.\n"
    "5. Reply in English, as plain prose with no headings or bullet points."
)


# ---- deterministic map (consulted before the model) ------------------------
#
# Messages we already understand. Each entry is (pattern, explanation). These
# exist so a known rejection reads identically every time and costs nothing -
# and so the two bugs tier 1 now prevents still produce a clear sentence if one
# ever reaches SAP by another route (a hand-built payload, a tenant whose flag
# means something different). Order matters: first match wins.
_KNOWN_ERRORS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r"only fill referencedocument.*if gr[- ]?based iv is active", re.I | re.S),
        "This purchase order line is invoiced directly against the order, not against "
        "a goods receipt, so the invoice must not carry a goods-receipt reference. "
        "The invoice was built with one. This is a bug in the posting payload rather "
        "than a problem with your invoice or the PO - report it, since the pre-flight "
        "check is supposed to prevent exactly this.",
    ),
    (
        re.compile(r"item\s+not\s+selectable", re.I),
        "SAP will not let this invoice select the purchase order line. That usually "
        "means the line is not flagged for invoice receipt, is already finally "
        "invoiced or deleted, or the invoice referenced it the wrong way (order vs "
        "goods receipt). Check the PO line in SAP before retrying.",
    ),
    (
        re.compile(r"goods receipt item .* is invalid", re.I),
        "The goods receipt this invoice points at is not a valid reference for the "
        "purchase order line - it may have been reversed or cancelled, or it belongs "
        "to a different line. Confirm which delivery this invoice covers.",
    ),
    (
        re.compile(r"(payment term|terms of payment).*(missing|not|required|enter)", re.I),
        "SAP needs payment terms to post the invoice and none are set. They normally "
        "come from the vendor master onto the purchase order. Ask for them to be "
        "maintained on the vendor master or the PO, then re-run the invoice.",
    ),
    (
        re.compile(r"balance not zero|balance.*not.*clear", re.I),
        "The invoice does not balance in SAP's eyes: the gross amount does not equal "
        "the line amounts plus tax. On this system the gross is set equal to the net "
        "total, which is only correct when the tax code is 0%-rated - a non-zero tax "
        "rate needs the gross amount adjusted upward before posting.",
    ),
    (
        re.compile(r"posting period .* (not open|closed)", re.I),
        "The accounting period for the posting date is closed, so nothing can be "
        "posted into it. Either use a posting date in an open period or ask Finance "
        "to open the period.",
    ),
    (
        re.compile(r"(tax code) .* (does not exist|not defined|invalid)", re.I),
        "The tax code on the invoice line does not exist in this company code's tax "
        "configuration. The correct code has to come from tax customizing - it cannot "
        "be guessed here.",
    ),
]


def match_known_error(raw_text: str) -> str:
    """Deterministic first pass. Returns "" when nothing matches."""
    for pattern, explanation in _KNOWN_ERRORS:
        if pattern.search(raw_text or ""):
            return explanation
    return ""


# ---- the bounded LLM fallback ---------------------------------------------


def translate_posting_error(
    sap_message: str,
    raw_body: str = "",
    provider: object | None = None,
) -> dict:
    """Explain one SAP rejection in plain language.

    Args:
        sap_message: the translated-but-still-SAP-shaped user message
            (SAPRequestError.user_message).
        raw_body: SAP's raw error payload, if available - richer text for the
            deterministic patterns to match against. Never shown to the user.
        provider: a generation provider exposing `.generate(system_prompt,
            user_prompt, max_tokens)`. Injected so tests run without Bedrock,
            and so this module never constructs a client of its own.

    Returns {explanation, source, ok}. `source` is one of:
        "known"       - matched the deterministic map (no LLM call made)
        "llm"         - translated by the model
        "untranslated"- no map hit and the model was unavailable or failed;
                        `explanation` is then SAP's own message, unchanged.

    Never raises. A translation layer that can fail loudly would turn a clear
    posting failure into an opaque one.
    """
    haystack = f"{sap_message}\n{raw_body}".strip()

    known = match_known_error(haystack)
    if known:
        return {"ok": True, "explanation": known, "source": "known"}

    if provider is None or not hasattr(provider, "generate"):
        return {"ok": True, "explanation": sap_message, "source": "untranslated"}

    excerpt = (raw_body or sap_message)[:_MAX_RAW_CHARS]
    user_prompt = (
        "A supplier invoice failed to post. SAP returned this error. Explain it "
        "in plain language for the person who submitted the invoice.\n\n"
        f"SAP error:\n{excerpt}"
    )
    try:
        result = provider.generate(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            max_tokens=_MAX_TOKENS,
        )
        explanation = (getattr(result, "answer", "") or "").strip()
    except Exception as exc:  # noqa: BLE001 - see docstring: never raise from here
        log.warning("Error translation failed (%s); returning SAP's own message.",
                    type(exc).__name__)
        return {"ok": True, "explanation": sap_message, "source": "untranslated"}

    if not explanation:
        return {"ok": True, "explanation": sap_message, "source": "untranslated"}
    return {"ok": True, "explanation": explanation, "source": "llm"}

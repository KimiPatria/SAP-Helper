"""Six scoring dimensions, all deterministic - no LLM judge.

An LLM judge would need a different model family than the one under test (to
avoid self-enhancement bias) plus calibration against human ratings before its
scores meant anything. That is a larger project than the failure modes here
warrant, because this architecture makes the important ones mechanically
checkable instead:

* The panel owns every figure and the tool result carries the rest, so
  "is every number in this answer traceable to the tool result?" is a REAL
  hallucination detector, not a proxy for one.
* Tool routing is enforced by the tool list, so the expected outcome is a set
  comparison rather than a judgment call.

Scores are 0.0-1.0 per dimension. A dimension that does not apply to a case
(refusal on a lookup question) returns None and is dropped from that case's
weighting, with the remaining weights renormalised - otherwise cases would be
scored against criteria they were never meant to satisfy.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

WEIGHTS = {
    "routing": 0.30,
    "number_fidelity": 0.25,
    "document_fidelity": 0.15,
    "refusal": 0.15,
    "language": 0.10,
    "efficiency": 0.05,
}

PASS_THRESHOLD = 0.70

# Per-dimension floors, checked across the whole run in addition to the
# weighted average. A single aggregate hides dimension-specific collapse: an
# agent that routes perfectly and invents every figure still averages well
# above threshold, because routing carries twice number_fidelity's weight.
#
# The two fidelity floors are the strictest because they are the ones that
# cause harm - a wrong tool choice produces a visibly unhelpful answer, while
# a fabricated quantity produces a confident, plausible, actionable lie.
DIMENSION_FLOORS = {
    "routing": 0.85,
    "number_fidelity": 0.95,
    "document_fidelity": 0.95,
    "refusal": 0.90,
    "language": 0.90,
    "efficiency": 0.80,
}


# ---- number extraction ---------------------------------------------------

_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
# SAP document numbers: purchase orders (45...), material documents (5...),
# supplier invoices (51...). Invented ones are the most damaging fabrication
# because they look authoritative and a human may act on them.
_DOC_NUMBER = re.compile(r"\b(?:45\d{8}|5\d{9})\b")

# Integers at or below this are counts, item numbers and ordinals ("the 2
# open lines", "item 10") - narrating those is legitimate. Above it, a number
# is a quantity, price or amount and must be traceable.
_COUNT_CEILING = Decimal("20")


def _normalise(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", "")).normalize()
    except (InvalidOperation, ValueError):
        return None


def significant_numbers(text: str) -> set[Decimal]:
    """Figures a reader would act on: anything with a decimal part, or any
    integer past the count ceiling. Dates are stripped first so '2026-07-01'
    does not decompose into three spurious numbers."""
    stripped = _ISO_DATE.sub(" ", text or "")
    found: set[Decimal] = set()
    for token in _NUMBER.findall(stripped):
        value = _normalise(token)
        if value is None:
            continue
        if value != value.to_integral_value() or abs(value) > _COUNT_CEILING:
            found.add(value)
    return found


def dates_in(text: str) -> set[str]:
    return set(_ISO_DATE.findall(text or ""))


def document_numbers(text: str) -> set[str]:
    return set(_DOC_NUMBER.findall(text or ""))


# ---- the dimensions ------------------------------------------------------


def score_routing(case, calls: list[str]) -> float:
    """Outcome, not path: did it reach for a defensible tool?

    A case with no expected tools but a populated allow list is genuinely
    ambiguous - any allowed tool (or a clarifying question) is correct.
    """
    called = set(calls)
    expected, allowed = set(case.expect_tools), set(case.allow_tools)

    if not expected:
        if not allowed:                       # must call nothing at all
            return 1.0 if not called else 0.0
        if not called:                        # asked for clarification instead
            return 1.0
        return 1.0 if called <= allowed else 0.0

    if not expected <= called:
        return 0.0                            # the required tool never ran
    extras = called - expected - allowed
    return 1.0 if not extras else 0.5         # right tool, plus noise


def score_number_fidelity(answer: str, tool_results: list[dict]) -> float | None:
    """Every significant figure in the answer must appear in a tool result or
    in the question. Returns None when the answer states no figures at all.

    Derived arithmetic counts as ungrounded on purpose. The design says the
    model narrates and analysis.py computes - a model deriving "80% received"
    is doing exactly the non-deterministic arithmetic the architecture removed
    from it, even when the sum happens to be right.
    """
    import json

    grounded = set()
    grounded_dates = set()
    for result in tool_results:
        blob = json.dumps(result, default=str)
        grounded |= significant_numbers(blob)
        grounded_dates |= dates_in(blob)

    claimed = significant_numbers(answer)
    claimed_dates = dates_in(answer)
    total = len(claimed) + len(claimed_dates)
    if total == 0:
        return None

    ok = len(claimed & grounded) + len(claimed_dates & grounded_dates)
    return ok / total


def score_document_fidelity(answer: str, question: str, tool_results: list[dict]) -> float | None:
    """SAP document numbers in the answer must have come from somewhere real."""
    import json

    claimed = document_numbers(answer)
    if not claimed:
        return None
    grounded = document_numbers(question)
    for result in tool_results:
        grounded |= document_numbers(json.dumps(result, default=str))
    return len(claimed & grounded) / len(claimed)


_REFUSAL_SIGNALS = (
    "knowledge base", "knowledge-base", "cannot answer", "can't answer",
    "not able to answer", "only handles", "only supports", "this interface",
    "transactional", "troubleshooting question", "outside", "not something i can",
)


def score_refusal(case, answer: str) -> float | None:
    """For knowledge questions the correct behaviour is to decline AND point at
    the other interface. Answering from the model's own SAP knowledge is the
    failure - it produces confident, ungrounded, uncitable advice."""
    if not case.expect_refusal:
        return None
    lowered = (answer or "").lower()
    return 1.0 if any(signal in lowered for signal in _REFUSAL_SIGNALS) else 0.0


# Stopword-frequency language identification. A dependency-free heuristic is
# enough here because the languages are known in advance and the failure being
# measured is gross (an English question answered in Portuguese), not subtle.
_STOPWORDS = {
    "en": {"the", "is", "and", "for", "with", "not", "have", "been", "this",
           "that", "was", "are", "has", "of", "on", "to", "it", "as", "but"},
    "id": {"yang", "dan", "untuk", "dengan", "tidak", "sudah", "belum", "ini",
           "itu", "adalah", "pada", "dari", "akan", "saja", "masih", "atau"},
    "es": {"el", "la", "los", "las", "de", "que", "y", "en", "para", "con",
           "no", "una", "por", "se", "del", "está", "han", "sido"},
    "pt": {"o", "a", "os", "as", "de", "que", "e", "em", "para", "com", "não",
           "uma", "por", "do", "da", "está", "foram", "sido"},
    "de": {"der", "die", "das", "und", "für", "mit", "nicht", "ist", "auf",
           "von", "zu", "den", "wurde", "sind", "eine", "im"},
}


def detect_language(text: str) -> str:
    words = re.findall(r"[a-zà-ÿ]+", (text or "").lower())
    if not words:
        return "unknown"
    counts = {
        code: sum(1 for w in words if w in stops)
        for code, stops in _STOPWORDS.items()
    }
    best = max(counts, key=counts.get)
    return best if counts[best] else "unknown"


def score_language(case, answer: str) -> float | None:
    """The known drift bug: the same English question comes back in English,
    Spanish or Portuguese across repeated runs. Measuring it turns 'sometimes
    it goes weird' into a rate that can be re-measured after a fix."""
    detected = detect_language(answer)
    if detected == "unknown":
        return None                    # too short to judge; don't invent signal
    return 1.0 if detected == case.language else 0.0


def score_efficiency(case, calls: list[str]) -> float:
    """Tool calls cost latency and tokens. Within budget is full marks; past it
    degrades rather than failing outright, since an extra call is a smell, not
    a wrong answer."""
    if len(calls) <= case.max_tool_calls:
        return 1.0
    over = len(calls) - case.max_tool_calls
    return max(0.0, 1.0 - 0.34 * over)


# ---- assembly ------------------------------------------------------------


def score_run(case, answer: str, calls: list[str], tool_results: list[dict]) -> dict:
    """Score one execution of one case. Returns per-dimension scores (None
    where inapplicable), the renormalised overall, and any hard rule breaches."""
    dimensions = {
        "routing": score_routing(case, calls),
        "number_fidelity": score_number_fidelity(answer, tool_results),
        "document_fidelity": score_document_fidelity(answer, case.question, tool_results),
        "refusal": score_refusal(case, answer),
        "language": score_language(case, answer),
        "efficiency": score_efficiency(case, calls),
    }

    applicable = {k: v for k, v in dimensions.items() if v is not None}
    total_weight = sum(WEIGHTS[k] for k in applicable) or 1.0
    overall = sum(WEIGHTS[k] * v for k, v in applicable.items()) / total_weight

    lowered = (answer or "").lower()
    breaches = []
    for phrase in case.must_mention:
        if phrase.lower() not in lowered:
            breaches.append(f"missing required phrase: {phrase!r}")
    for phrase in case.must_not_mention:
        if phrase.lower() in lowered:
            breaches.append(f"contains forbidden phrase: {phrase!r}")

    # A content breach is a real failure regardless of a good weighted average:
    # "I have created the purchase order" scores well on routing and language
    # while being the worst possible answer.
    if breaches:
        overall = min(overall, 0.5)

    return {
        "dimensions": dimensions,
        "overall": round(overall, 4),
        "passed": overall >= PASS_THRESHOLD and not breaches,
        "breaches": breaches,
    }

"""Variance notification email - Amazon SES, with hard guards.

When an invoice does not match, a human needs to know in plain language. The
LLM is good at the plain language and untrustworthy with numbers, so the two
are strictly separated:

* every figure in the email is rendered HERE, server-side, from the variance
  struct the matcher produced (`render_variance_facts`);
* the model writes ONLY the surrounding prose, in a pinned language;
* before anything is sent, a numeral guard scans the model's prose and blocks
  the send if it contains any number that is not among the server-rendered
  facts - a fabricated amount can never reach a recipient.

Three more safety rails from the brief:
* SES stays in SANDBOX mode - it only delivers to verified addresses. That is
  the safety net, not a limitation to engineer around.
* a hard per-run send cap; a breach halts sending and logs rather than
  partially blasting (SESMailer refuses once the cap is reached).
* a dedup key (vendor + PO + item + variance types + rounded amounts) checked
  against a ledger, so the same discrepancy never emails twice.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation

log = logging.getLogger("sap-mcp.invoice-email")

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


class EmailError(Exception):
    """Raised for a configuration/transport failure the caller should surface."""


# ---- number handling (the guard's backbone) -------------------------------


def _canon(token: str) -> str:
    """Canonicalise a numeric token so '100', '100.0' and '100.00' compare
    equal, and thousands separators don't matter."""
    cleaned = token.replace(",", "")
    try:
        return format(Decimal(cleaned).normalize(), "f")
    except InvalidOperation:
        return cleaned


def extract_numbers(text: str) -> set[str]:
    return {_canon(t) for t in _NUMBER_RE.findall(text.replace(",", ""))}


def numbers_are_safe(prose: str, facts: str) -> tuple[bool, list[str]]:
    """The prose may only contain numbers that also appear in the facts block.
    Returns (ok, offending_numbers)."""
    allowed = extract_numbers(facts)
    offending = sorted({_canon(t) for t in _NUMBER_RE.findall(prose.replace(",", ""))} - allowed)
    return (not offending, offending)


# ---- server-rendered facts ------------------------------------------------


def render_variance_facts(match_record: dict) -> str:
    """Deterministic, numbers-authoritative block. Every figure the email is
    allowed to state lives here, rendered from the struct - never the model."""
    computed = match_record.get("computed", {})
    lines = [
        "Verified facts (system-generated):",
        f"  Purchase order:      {match_record.get('purchase_order', '')}",
        f"  PO line item:        {computed.get('po_item', '')}",
        f"  Material:            {computed.get('material', '')}",
        f"  Vendor invoice no.:  {match_record.get('vendor_invoice_number', '')}",
        f"  Ordered quantity:    {computed.get('ordered_qty', '')} {computed.get('unit', '')}",
        f"  Received quantity:   {computed.get('received_qty', '')} {computed.get('unit', '')}",
        f"  Invoiced quantity:   {computed.get('invoiced_qty', '')} {computed.get('unit', '')}",
        f"  PO unit price:       {computed.get('po_unit_price', '')} {computed.get('currency', '')}",
        f"  Invoice unit price:  {computed.get('invoice_unit_price', '')} {computed.get('currency', '')}",
        "",
        "Discrepancies detected:",
    ]
    for variance in match_record.get("variances", []):
        lines.append(f"  - {variance.get('type', '')}: {variance.get('detail', '')}")
    return "\n".join(lines)


def dedup_amounts(match_record: dict) -> list[float]:
    """Stable numeric fingerprint of the variance, for the dedup key."""
    computed = match_record.get("computed", {})
    out: list[float] = []
    for key in ("invoiced_qty", "received_qty", "invoice_unit_price", "po_unit_price"):
        try:
            out.append(round(float(computed.get(key, "") or 0), 2))
        except (TypeError, ValueError):
            out.append(0.0)
    return out


# ---- prose generation (model writes words, not numbers) -------------------


def build_prose_prompts(match_record: dict, language: str) -> tuple[str, str]:
    variance_types = ", ".join(match_record.get("variance_types", [])) or "unmatched"
    system = (
        f"You write a short, professional accounts-payable email body in {language}. "
        f"Always write in {language} regardless of any other language in the input. "
        "A supplier invoice could not be automatically matched to its purchase order "
        "and goods receipt. Write ONLY prose: a brief greeting, one or two sentences "
        "explaining that the invoice needs manual review and the general nature of the "
        "problem, and a closing line asking the recipient to review it. "
        "CRITICAL: do NOT write any specific numbers, amounts, quantities, prices, "
        "dates, percentages, or document numbers - a verified facts table is appended "
        "separately and owns all figures. Do not invent data. Do not use placeholders "
        "like [amount]. Keep it under 100 words."
    )
    user = (
        f"The discrepancy type(s): {variance_types}. "
        "Write the email body now, following every rule. No numbers in your text."
    )
    return system, user


def assemble_email(prose: str, facts: str) -> str:
    return f"{prose.strip()}\n\n{'-' * 56}\n{facts}"


# ---- SES send with the per-run cap ----------------------------------------


class SESMailer:
    """Thin SES wrapper that enforces the per-run send cap. Once `cap` sends
    have happened, further sends are refused entirely (halt, don't partial-send).

    Takes AWS keys explicitly, like every other boto3 client in this codebase
    (BedrockProvider, BedrockKBRetriever, credentials._from_secrets_manager).
    That is not optional politeness: values in `.env` are read by
    pydantic-settings and are NOT process environment variables, so a client
    built without them falls through to boto3's default chain and fails with
    "Unable to locate credentials" on any machine without ~/.aws or real AWS_*
    env vars. Empty strings still hand off to the default chain, which is the
    right behaviour under an instance role.
    """

    def __init__(
        self,
        region: str,
        sender: str,
        cap: int,
        aws_access_key_id: str = "",
        aws_secret_access_key: str = "",
    ):
        self._region = region
        self._sender = sender
        self._cap = cap
        self._access_key = aws_access_key_id
        self._secret_key = aws_secret_access_key
        self._sent = 0
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            try:
                import boto3
                self._client = boto3.client(
                    "ses",
                    region_name=self._region,
                    aws_access_key_id=self._access_key or None,
                    aws_secret_access_key=self._secret_key or None,
                )
            except Exception as exc:  # boto3 import / client construction
                raise EmailError(f"Could not initialise SES client: {exc}") from exc
        return self._client

    def send(self, recipients: list[str], subject: str, body: str) -> dict:
        if not self._sender:
            raise EmailError("SES sender (SES_SENDER) is not configured.")
        if not recipients:
            raise EmailError("No verified recipients configured (INVOICE_EMAIL_RECIPIENTS).")
        if self._sent >= self._cap:
            # Halt: do not send. The caller logs this; nothing partial goes out.
            raise EmailError(
                f"Per-run email send cap ({self._cap}) reached - halting without "
                "sending. No partial sends."
            )
        client = self._ensure_client()
        try:
            import botocore.exceptions
            response = client.send_email(
                Source=self._sender,
                Destination={"ToAddresses": recipients},
                Message={
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                },
            )
        except botocore.exceptions.ClientError as exc:
            error = exc.response.get("Error", {})
            code = error.get("Code", "unknown")
            hint = ""
            if code in ("MessageRejected", "MailFromDomainNotVerified"):
                hint = (" In SES sandbox both sender AND every recipient must be "
                        "verified addresses - verify them in the SES console.")
            raise EmailError(f"SES rejected the send ({code}): {error.get('Message', exc)}.{hint}") from exc
        except botocore.exceptions.BotoCoreError as exc:
            raise EmailError(f"SES transport error: {exc}") from exc
        self._sent += 1
        return {"message_id": response.get("MessageId", ""), "sends_used": self._sent}


def send_variance_email(
    *,
    provider: object,
    match_record: dict,
    recipients: list[str],
    language: str,
    mailer: SESMailer,
    dedup_ledger,
    max_tokens: int = 400,
) -> dict:
    """Full guarded send for one variance record. Returns a structured result;
    never raises for an expected condition (no variance, duplicate, blocked
    prose) - only genuine transport/config errors surface via EmailError,
    which the caller turns into {ok: False}."""
    from app.mcp.invoice_ledger import EmailDedupLedger  # local import: avoid cycle

    if match_record.get("matched") or not match_record.get("variances"):
        return {"ok": True, "sent": False, "reason": "No variance to report."}

    key = EmailDedupLedger.dedup_key(
        vendor=str(match_record.get("po_supplier", "") or match_record.get("invoice_vendor_name", "")),
        purchase_order=str(match_record.get("purchase_order", "")),
        item=str(match_record.get("computed", {}).get("po_item", "")),
        variance_types=list(match_record.get("variance_types", [])),
        rounded_amounts=dedup_amounts(match_record),
    )
    existing = dedup_ledger.already_sent(key)
    if existing:
        return {
            "ok": True, "sent": False, "dedup_key": key,
            "reason": f"Already emailed this exact discrepancy at {existing['sent_at']}.",
        }

    if not recipients:
        return {"ok": False, "sent": False, "reason": "No verified SES recipients configured."}

    facts = render_variance_facts(match_record)
    if not hasattr(provider, "generate"):
        return {"ok": False, "sent": False, "reason": "Generation provider unavailable for prose."}
    system, user = build_prose_prompts(match_record, language)
    try:
        prose = provider.generate(system, user, max_tokens).answer
    except Exception as exc:
        return {"ok": False, "sent": False, "reason": f"Could not generate email prose: {exc}"}

    ok, offending = numbers_are_safe(prose, facts)
    if not ok:
        # Block: the model put numbers in prose that aren't verified facts.
        log.warning("Variance email blocked - prose contained non-fact numbers: %s", offending)
        return {
            "ok": False, "sent": False, "dedup_key": key,
            "reason": "Blocked: the drafted prose contained numbers not present in "
                      "the verified facts.",
            "offending_numbers": offending,
        }

    subject = (
        f"Invoice variance - PO {match_record.get('purchase_order', '')} "
        f"({', '.join(match_record.get('variance_types', []))})"
    )
    body = assemble_email(prose, facts)
    try:
        sent = mailer.send(recipients, subject, body)
    except EmailError as exc:
        return {"ok": False, "sent": False, "dedup_key": key, "reason": str(exc)}

    dedup_ledger.record(key, recipients, subject)
    log.info("Variance email sent (%s) to %s", sent.get("message_id", ""), recipients)
    return {
        "ok": True, "sent": True, "dedup_key": key, "subject": subject,
        "recipients": recipients, "message_id": sent.get("message_id", ""),
        "body_preview": body[:400],
    }

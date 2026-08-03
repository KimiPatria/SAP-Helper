"""Single-use proposal store for supplier-invoice posting - the write boundary.

Same mechanism and rationale as the PO PreviewStore (previews.py): the
`propose_*` step deposits the fully-built payload here and returns only a
`proposal_id`; `commit_*` accepts nothing but that id, so the model can never
hand the commit step a payload it invented. Entries are in-memory, single-use,
and expire after a TTL - a restart safely invalidates pending proposals.

It carries a little more than the PO store: alongside the payload it keeps the
duplicate-identity context (vendor + vendor invoice number + supplier + PO) so
the commit step can write the idempotency/duplicate ledger without re-deriving
it from the payload.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class ProposalEntry:
    proposal_id: str
    payload: dict
    payload_hash: str
    summary: str
    vendor: str
    vendor_invoice_number: str
    invoicing_party: str
    purchase_order: str
    created_at: float = field(default_factory=time.monotonic)
    consumed: bool = False
    # Carried so a pre-commit edit (update_proposal_fields) can rebuild the
    # payload/summary/fields under the SAME proposal_id rather than minting a
    # new one - commit still takes only an id (see invoice_tools.py docstring).
    proposed: object = None          # the ProposedInvoice this entry was built from
    confidences: dict = field(default_factory=dict)   # OCR confidences, by extracted-field name
    match_summary: str = ""
    warnings: list = field(default_factory=list)


class InvoiceProposalStore:
    def __init__(self, ttl_seconds: int):
        self._ttl = ttl_seconds
        self._entries: dict[str, ProposalEntry] = {}

    def put(
        self,
        *,
        payload: dict,
        payload_hash: str,
        summary: str,
        vendor: str,
        vendor_invoice_number: str,
        invoicing_party: str,
        purchase_order: str,
        proposed: object = None,
        confidences: dict | None = None,
        match_summary: str = "",
        warnings: list | None = None,
    ) -> ProposalEntry:
        self._prune()
        entry = ProposalEntry(
            proposal_id=uuid.uuid4().hex,
            payload=payload,
            payload_hash=payload_hash,
            summary=summary,
            vendor=vendor,
            vendor_invoice_number=vendor_invoice_number,
            invoicing_party=invoicing_party,
            purchase_order=purchase_order,
            proposed=proposed,
            confidences=confidences or {},
            match_summary=match_summary,
            warnings=warnings or [],
        )
        self._entries[entry.proposal_id] = entry
        return entry

    def update(
        self, proposal_id: str, *, payload: dict, payload_hash: str, summary: str, proposed: object,
    ) -> ProposalEntry | None:
        """Rewrite a not-yet-committed entry's payload in place, keeping the
        same proposal_id (used by update_proposal_fields - a human edit before
        approval, not a new proposal). Returns None if the id is gone, already
        consumed, or expired - same conditions as `claim`."""
        entry = self._entries.get(proposal_id)
        if entry is None or entry.consumed:
            return None
        if time.monotonic() - entry.created_at > self._ttl:
            del self._entries[proposal_id]
            return None
        entry.payload = payload
        entry.payload_hash = payload_hash
        entry.summary = summary
        entry.proposed = proposed
        return entry

    def claim(self, proposal_id: str) -> tuple[ProposalEntry | None, str]:
        entry = self._entries.get(proposal_id)
        if entry is None:
            return None, (
                "Unknown proposal_id. It may have expired, or the server was "
                "restarted since the proposal was made. Re-run the match to get "
                "a fresh proposal and re-confirm before posting."
            )
        if entry.consumed:
            return None, (
                "This proposal was already posted - the supplier invoice exists. "
                "Do not post it again."
            )
        if time.monotonic() - entry.created_at > self._ttl:
            del self._entries[proposal_id]
            return None, (
                "This proposal has expired. Re-run the match and re-confirm with "
                "a human before posting."
            )
        return entry, ""

    def mark_consumed(self, proposal_id: str) -> None:
        entry = self._entries.get(proposal_id)
        if entry:
            entry.consumed = True

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [k for k, e in self._entries.items() if now - e.created_at > self._ttl]:
            del self._entries[key]

"""Durable JSON ledgers for the Supplier Invoice pipeline.

Two ledgers, same pattern as the PO idempotency ledger (registry.py) and the
ingestion ledger: a JSON file is the simplest thing that demonstrates the
mechanism; at production scale each becomes a table with identical operations.

1. `SupplierInvoiceLedger` - the invoice's memory. It answers two questions:
   * "have we already handled this vendor's invoice?" (the DUPLICATE check,
     run before anything else) keyed on vendor + the vendor's own invoice
     number - the identity a human uses to say "that's the same bill";
   * "did this exact approved proposal already post?" (commit IDEMPOTENCY)
     keyed on the proposal id, so a retried commit returns the SupplierInvoice
     SAP already assigned instead of posting a twin.

2. `EmailDedupLedger` - the variance email's memory, so the same discrepancy
   doesn't email a person twice. Keyed on vendor + PO + item + variance type +
   rounded amounts (the brief's dedup key).

The duplicate identity uses the *extracted* vendor text and vendor invoice
number - both read straight off the document - so re-submitting the same
photo/PDF produces the same key whether or not the PO lookup has run yet.

Each entry carries a SECOND key on the same reference: the supplier code
(`InvoicingParty`) resolved from the PO. Two keys because the two identities
become available at different times and fail differently - the vendor text is
known immediately but drifts with OCR ("Acme Ltd" vs "Acme Limited"), while the
supplier code is stable but unknown until the PO lookup. Checking the text key
first stops a resubmission before any SAP read; checking the party key after the
lookup catches the same bill re-read under a slightly different name.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def _norm(text: object) -> str:
    """Fold a free-text token to a stable identity: uppercase, keep only
    alphanumerics. 'Acme Corp.' and 'ACME  CORP' collapse to the same thing;
    'INV-0042' and 'inv 0042' likewise."""
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


class SupplierInvoiceLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._entries: dict[str, dict] = {}
        if self.path.exists():
            self._entries = json.loads(self.path.read_text(encoding="utf-8"))

    # ---- duplicate identity (vendor + vendor's invoice number) ----------

    @staticmethod
    def duplicate_key(vendor: str, vendor_invoice_number: str) -> str:
        return hashlib.sha256(
            f"{_norm(vendor)}|{_norm(vendor_invoice_number)}".encode()
        ).hexdigest()

    def find_duplicate(self, vendor: str, vendor_invoice_number: str) -> dict | None:
        """Most recent handled invoice matching this vendor TEXT + reference, or
        None. A hit STOPS the pipeline and surfaces the existing document -
        it never silently skips."""
        return self._most_recent(
            "dedup_key", self.duplicate_key(vendor, vendor_invoice_number)
        )

    def find_duplicate_by_party(
        self, invoicing_party: str, vendor_invoice_number: str
    ) -> dict | None:
        """Same question keyed on the SAP supplier code instead of the vendor
        text - the stable identity, available once the PO lookup has run. This is
        what catches a resubmission whose vendor name was OCR'd differently."""
        if not (invoicing_party and vendor_invoice_number):
            return None
        return self._most_recent(
            "party_key", self.duplicate_key(invoicing_party, vendor_invoice_number)
        )

    def _most_recent(self, field: str, key: str) -> dict | None:
        matches = [e for e in self._entries.values() if e.get(field) == key]
        return max(matches, key=lambda e: e.get("created_at", "")) if matches else None

    # ---- commit idempotency (per approved proposal) ---------------------

    def find_by_proposal(self, proposal_id: str) -> dict | None:
        for entry in self._entries.values():
            if entry.get("proposal_id") == proposal_id:
                return entry
        return None

    @staticmethod
    def key(payload_hash: str, proposal_id: str) -> str:
        return hashlib.sha256(f"{payload_hash}:{proposal_id}".encode()).hexdigest()

    def find(self, key: str) -> dict | None:
        return self._entries.get(key)

    def record(
        self,
        *,
        payload_hash: str,
        proposal_id: str,
        vendor: str,
        vendor_invoice_number: str,
        supplier_invoice: str,
        fiscal_year: str,
        invoicing_party: str = "",
        purchase_order: str = "",
    ) -> dict:
        entry = {
            # Two identities for the same bill - see the module docstring. `vendor`
            # MUST be the vendor text read off the document, or find_duplicate()
            # (which is handed exactly that) can never match what we recorded.
            "dedup_key": self.duplicate_key(vendor, vendor_invoice_number),
            "party_key": self.duplicate_key(invoicing_party, vendor_invoice_number),
            "proposal_id": proposal_id,
            "payload_hash": payload_hash,
            "vendor": vendor,
            "vendor_invoice_number": vendor_invoice_number,
            "invoicing_party": invoicing_party,
            "purchase_order": purchase_order,
            "supplier_invoice": supplier_invoice,
            "fiscal_year": fiscal_year,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._entries[self.key(payload_hash, proposal_id)] = entry
        self._save()
        return entry

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._entries, indent=2), encoding="utf-8")


class EmailDedupLedger:
    """Records variance emails already sent, so an identical discrepancy is
    not re-mailed. Dedup key = vendor + PO + item + variance type + rounded
    amounts (rounding so 100.001 and 100.004 count as the same variance)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._entries: dict[str, dict] = {}
        if self.path.exists():
            self._entries = json.loads(self.path.read_text(encoding="utf-8"))

    @staticmethod
    def dedup_key(
        vendor: str,
        purchase_order: str,
        item: str,
        variance_types: list[str],
        rounded_amounts: list[float],
    ) -> str:
        parts = [
            _norm(vendor),
            _norm(purchase_order),
            _norm(item),
            ",".join(sorted(variance_types)),
            ",".join(f"{round(a, 2):.2f}" for a in rounded_amounts),
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    def already_sent(self, key: str) -> dict | None:
        return self._entries.get(key)

    def record(self, key: str, recipients: list[str], summary: str) -> None:
        self._entries[key] = {
            "recipients": recipients,
            "summary": summary,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._entries, indent=2), encoding="utf-8")

"""Supplier Invoice read access - API_SUPPLIERINVOICE_PROCESS_SRV (OData v2).

Read-only, mirrors gr.py. Its one job is the live half of the duplicate check:
ask SAP whether an invoice with this vendor reference already exists for this
supplier, so a duplicate is caught even if it was posted outside this tool (the
JSON ledger only knows what *this* pipeline posted). The ledger stays the fast,
offline first line; this is the authoritative cross-check when SAP is reachable.
"""

from __future__ import annotations

from app.mcp.client import SAPClient

_HEADER_ENTITY_SET = "A_SupplierInvoice"
_HEADER_FIELDS = (
    "SupplierInvoice,FiscalYear,CompanyCode,InvoicingParty,"
    "SupplierInvoiceIDByInvcgParty,DocumentCurrency,InvoiceGrossAmount,"
    "SupplierInvoiceStatus,PostingDate"
)


def fetch_invoices_by_reference(
    client: SAPClient,
    service_root: str,
    invoicing_party: str,
    vendor_invoice_number: str,
) -> list[dict]:
    """Existing supplier invoices matching this supplier + vendor reference.

    Both filters are needed: a vendor reference is only unique within a
    supplier. Returns raw A_SupplierInvoice header rows ({"d":{"results":[...]}}
    v2 envelope); an empty list means SAP has no such document.
    """
    party = invoicing_party.replace("'", "''")
    reference = vendor_invoice_number.replace("'", "''")
    data = client.get(
        f"{service_root}/{_HEADER_ENTITY_SET}",
        params={
            "$filter": (
                f"InvoicingParty eq '{party}' and "
                f"SupplierInvoiceIDByInvcgParty eq '{reference}'"
            ),
            "$select": _HEADER_FIELDS,
        },
        context=(
            f"checking SAP for an existing invoice {vendor_invoice_number} "
            f"from supplier {invoicing_party}"
        ),
    )
    return data.get("d", {}).get("results") or []

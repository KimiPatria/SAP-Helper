"""SAP transaction layer - an MCP server for creating SAP documents.

Exposes tools that let an LLM look up master data, preview a Purchase
Order resolved from natural language, and (only after explicit user
confirmation of that preview) create it in S/4HANA Cloud via OData.

Layout:
    client.py    - SAPClient: Basic Auth reads, CSRF-token handshake for
                   writes, auth isolated behind one method for a later
                   OAuth swap
    errors.py    - translates verbose SAP OData error payloads into
                   plain-language messages (raw payload goes to logs only)
    resolver.py  - free-text -> ranked master-data candidates (suppliers,
                   materials, plants); never silently picks one
    po.py        - PO payload building, preview rendering, status parsing
    previews.py  - in-memory single-use preview store: the create tool
                   only accepts a preview_id minted here
    registry.py  - JSON idempotency ledger (same pattern as the
                   ingestion ledger) so retries don't create duplicate POs
    server.py    - the FastMCP server wiring the four tools together;
                   run with `python -m app.mcp.server`

Goods Receipt and Supplier Invoice creation are deliberate later
extensions of the same patterns - see README.md in this folder.
"""

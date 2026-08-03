"""API request/response schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.config import settings


class HistoryTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


def _trim_history(value: list[HistoryTurn]) -> list[HistoryTurn]:
    # Degrade gracefully rather than 422 on an oversized/tampered client
    # payload - keep only the most recent turns. Shared by both request models.
    return value[-settings.history_max_turns :]


class QueryRequest(BaseModel):
    question: str = Field(min_length=3, max_length=4000)
    top_k: int | None = Field(default=None, ge=1, le=20)
    history: list[HistoryTurn] = Field(default_factory=list)
    # Which knowledge base to retrieve from for this query. None -> server
    # default (settings.knowledge_base). Lets the chat UI toggle per-request
    # without a server restart.
    knowledge_base: Literal["local", "bedrock"] | None = None

    @field_validator("history")
    @classmethod
    def _cap_history(cls, value: list[HistoryTurn]) -> list[HistoryTurn]:
        return _trim_history(value)


class McpQueryRequest(BaseModel):
    # The SAP MCP chat: no retrieval, no knowledge-base selection - just the
    # question and prior turns fed to the tool-calling provider.
    question: str = Field(min_length=3, max_length=4000)
    history: list[HistoryTurn] = Field(default_factory=list)
    # Ids of files uploaded via POST /api/attachments and attached to THIS turn.
    # Ids only - the bytes stay server-side in the attachment store, so nothing
    # large travels through the chat payload and the model cannot invent one.
    # A non-empty list is what unlocks the invoice tool for the turn.
    attachments: list[str] = Field(default_factory=list, max_length=4)
    # Client-minted trace id for this turn. The UI starts polling
    # /api/invoice/trace/{run_id} the moment it sends, so an invoice run's steps
    # stream in live instead of arriving as a finished list. Cosmetic if the turn
    # runs no pipeline; the server never trusts it for anything but trace keying.
    run_id: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9_-]*$")

    @field_validator("history")
    @classmethod
    def _cap_history(cls, value: list[HistoryTurn]) -> list[HistoryTurn]:
        return _trim_history(value)


class ToleranceUpdate(BaseModel):
    """Runtime override of any subset of the four matching-tolerance floors
    (POST /api/invoice/tolerances). All optional; omitted floors are unchanged."""
    price_pct: float | None = Field(default=None, ge=0, le=1)
    price_abs: float | None = Field(default=None, ge=0)
    quantity_pct: float | None = Field(default=None, ge=0, le=1)
    quantity_abs: float | None = Field(default=None, ge=0)


class ReviewRetry(BaseModel):
    """A human-corrected PO number to retry a parked review item with
    (POST /api/invoice/review/{review_id}/retry) - see
    invoice_tools.retry_match_with_corrected_po."""
    corrected_po_number: str


class ProposalEdit(BaseModel):
    """Human edits to a not-yet-approved supplier-invoice proposal
    (POST /api/invoice/proposal/{proposal_id}/edit). Keyed by the `key` field
    from each row in the proposal's `fields` list; PurchaseOrder/PurchaseOrderItem
    are rejected server-side even if sent (see invoice_posting.apply_field_edits)."""
    edits: dict[str, str]


class CitationOut(BaseModel):
    note_id: str
    title: str
    url: str
    component: str
    source_file: str
    score: float
    sections: list[str]


class ChunkOut(BaseModel):
    note_id: str
    section: str
    score: float
    text: str


class QueryResponse(BaseModel):
    answer: str | None
    error: str | None = None
    citations: list[CitationOut]
    chunks: list[ChunkOut]
    provider: str
    model: str
    knowledge_base: str
    latency_ms: int
    # Structured results too rich to read as chat prose (currently only an
    # invoice run). The UI renders a card per panel that opens a side drawer;
    # every figure the user sees comes from here, not from the model's text.
    panels: list[dict] = Field(default_factory=list)


class StatusResponse(BaseModel):
    ok: bool
    provider: str
    model: str
    embedding_model: str
    vector_store: str
    collection: str
    indexed_chunks: int
    indexed_documents: int
    # Which knowledge bases are actually usable right now, keyed by the same
    # values QueryRequest.knowledge_base accepts - drives the UI toggle.
    knowledge_bases: dict[str, bool]
    default_knowledge_base: str
    # SAP MCP function status (the second chat interface). Its provider is
    # always Bedrock/Nova, built independently of the generation provider above.
    mcp_ok: bool
    mcp_provider: str
    mcp_model: str
    # Whether the live SAP tenant is configured (SAP_BASE_URL set).
    sap_configured: bool


class IngestResponse(BaseModel):
    processed: int
    skipped: int
    failed: int
    chunks: int

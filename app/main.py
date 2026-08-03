"""API layer.

One core endpoint (POST /api/query) plus a status endpoint for the UI and
an ingest trigger for demo convenience. Serves the chat UI from /static.

Run:  uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.generation.base import GenerationError
from app.generation.condense import condense_question
from app.generation.factory import build_provider
from app.generation.prompts import (
    SAP_MCP_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_mcp_user_prompt,
    build_user_prompt,
)
from app.ingestion.ledger import IngestionLedger
from app.ingestion.pipeline import run_ingestion
from app.ingestion.sources import LocalFolderSource
from app.mcp.attachments import attachment_store
from app.mcp.credentials import is_configured as sap_credentials_configured
from app.mcp.invoice_tools import (
    commit_post_supplier_invoice,
    get_tolerances,
    list_review_queue,
    propose_posting_for_match,
    retry_match_with_corrected_po,
    run_invoice_pipeline,
    update_proposal_fields,
    update_tolerances,
)
from app.mcp.invoice_trace import trace_store
from app.mcp.tools import (
    build_execute_tool,
    build_tool_specs,
    run_supplier_invoice_match,
    wants_invoice_check,
)
from app.models import (
    ChunkOut,
    CitationOut,
    IngestResponse,
    McpQueryRequest,
    ProposalEdit,
    QueryRequest,
    QueryResponse,
    ReviewRetry,
    StatusResponse,
    ToleranceUpdate,
)
from app.retrieval.bedrock_kb import BedrockKBRetriever
from app.retrieval.embedder import build_embedder
from app.retrieval.retriever import RetrievalError, Retriever
from app.retrieval.store import VectorStore

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("sap-helper")

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# What the chat composer's attach button accepts. Kept in step with the invoice
# extractor's own supported types (app/mcp/invoice_extraction.py) - rejecting at
# upload gives a clear error instead of one buried in a tool result.
_ALLOWED_ATTACHMENT_TYPES = frozenset({
    "image/jpeg", "image/jpg", "image/png", "application/pdf",
})


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Loading embedding model %s ...", settings.embedding_model)
    embedder = build_embedder(settings.embedding_model)
    store = VectorStore(
        collection=settings.qdrant_collection,
        dim=embedder.dim,
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        path=settings.qdrant_path,
    )
    app.state.embedder = embedder
    app.state.store = store

    retrievers: dict[str, object] = {
        "local": Retriever(embedder, store, settings.sap_note_url_template)
    }
    retriever_errors: dict[str, str] = {}
    if settings.bedrock_kb_id:
        try:
            retrievers["bedrock"] = BedrockKBRetriever(
                knowledge_base_id=settings.bedrock_kb_id,
                aws_region=settings.aws_region,
                aws_access_key_id=settings.aws_access_key_id,
                aws_secret_access_key=settings.aws_secret_access_key,
            )
            log.info("Bedrock Knowledge Base retriever ready (kb id %s)", settings.bedrock_kb_id)
        except Exception as exc:  # boto3 import/client-construction failures
            retriever_errors["bedrock"] = str(exc)
            log.warning("Bedrock Knowledge Base unavailable: %s", exc)
    else:
        retriever_errors["bedrock"] = "BEDROCK_KB_ID not configured"
    app.state.retrievers = retrievers
    app.state.retriever_errors = retriever_errors

    try:
        app.state.provider = build_provider(settings)
        app.state.provider_error = None
        log.info("Generation provider: %s (%s)", app.state.provider.name, app.state.provider.model)
    except GenerationError as exc:
        # Retrieval still works without a generation backend; surface the
        # configuration problem instead of refusing to start.
        app.state.provider = None
        app.state.provider_error = str(exc)
        log.warning("Generation provider unavailable: %s", exc)

    # The SAP MCP chat (POST /api/mcp/query) always answers with Bedrock/Nova,
    # independent of GENERATION_PROVIDER above, since only Bedrock implements
    # tool-calling. Built separately so the two interfaces never share a
    # provider or leak tools into each other.
    try:
        from app.generation.bedrock import BedrockProvider

        app.state.mcp_provider = BedrockProvider(
            aws_region=settings.aws_region,
            model_id=settings.bedrock_model_id,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
        app.state.mcp_provider_error = None
        log.info("SAP MCP provider: %s (%s)", app.state.mcp_provider.name, app.state.mcp_provider.model)
    except Exception as exc:  # boto3 import / client construction failures
        app.state.mcp_provider = None
        app.state.mcp_provider_error = str(exc)
        log.warning("SAP MCP provider unavailable: %s", exc)
    yield


app = FastAPI(title="SAP Helper POC", lifespan=lifespan)


@app.post("/api/query", response_model=QueryResponse)
def query(req: QueryRequest) -> QueryResponse:
    started = time.perf_counter()
    provider = app.state.provider

    top_k = req.top_k or settings.top_k
    kb_name = req.knowledge_base or settings.knowledge_base
    retriever = app.state.retrievers.get(kb_name)
    if retriever is None:
        detail = app.state.retriever_errors.get(kb_name, f"unknown knowledge base '{kb_name}'")
        return QueryResponse(
            answer=None,
            error=f"Knowledge base '{kb_name}' is not available: {detail}",
            citations=[],
            chunks=[],
            provider=provider.name if provider else settings.generation_provider,
            model=provider.model if provider else "-",
            knowledge_base=kb_name,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    retrieval_query = req.question
    if provider is not None:
        # Runs even without history: the condense step also translates
        # non-English questions to English for the English-only embedder.
        try:
            retrieval_query = condense_question(
                provider, req.history or [], req.question, settings.condense_max_tokens
            )
        except GenerationError as exc:
            # A condensation failure must never fail the whole request -
            # retrieval still works on the raw question, just less precisely.
            log.warning("Query condensation failed, using raw question: %s", exc)

    try:
        chunks, citations = retriever.retrieve(retrieval_query, top_k)
    except RetrievalError as exc:
        log.warning("Retrieval failed for knowledge base '%s': %s", kb_name, exc)
        return QueryResponse(
            answer=None,
            error=str(exc),
            citations=[],
            chunks=[],
            provider=provider.name if provider else settings.generation_provider,
            model=provider.model if provider else "-",
            knowledge_base=kb_name,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    answer: str | None = None
    error: str | None = None
    if provider is None:
        error = f"Generation backend not configured: {app.state.provider_error}"
    elif not chunks:
        # This endpoint is document-retrieval only - no live SAP tools - so
        # with no excerpts there is nothing to answer from. (Transactional
        # lookups live at POST /api/mcp/query.)
        answer = (
            "No relevant SAP Notes were found in the indexed corpus for this "
            "problem description. Try rephrasing with the error message, "
            "transaction code, or component involved."
        )
    else:
        user_prompt = build_user_prompt(req.question, chunks, req.history, settings.history_char_budget)
        try:
            result = provider.generate(SYSTEM_PROMPT, user_prompt, settings.generation_max_tokens)
            answer = result.answer
        except GenerationError as exc:
            # Sources are still valuable when generation fails - return them.
            error = str(exc)
            log.warning("Generation failed: %s", exc)

    return QueryResponse(
        answer=answer,
        error=error,
        citations=[CitationOut(**c.__dict__ | {"sections": list(c.sections)}) for c in citations],
        chunks=[
            ChunkOut(note_id=c.note_id, section=c.section, score=round(c.score, 4), text=c.text)
            for c in chunks
        ],
        provider=provider.name if provider else settings.generation_provider,
        model=provider.model if provider else "-",
        knowledge_base=kb_name,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


@app.post("/api/mcp/query", response_model=QueryResponse)
def mcp_query(req: McpQueryRequest) -> QueryResponse:
    """The SAP MCP chat: live, read-only transactional lookups only. No
    retrieval and no SAP Note citations - the Bedrock/Nova provider decides
    which (if any) tool to call and answers from the live result."""
    started = time.perf_counter()
    provider = app.state.mcp_provider

    answer: str | None = None
    error: str | None = None
    panels: list[dict] = []
    if provider is None:
        error = f"SAP MCP backend not configured: {app.state.mcp_provider_error}"
    else:
        # Resolve attachment ids to metadata for the prompt. Unknown/expired ids
        # are dropped here so the model is never told about a file it cannot use.
        attachments = []
        for attachment_id in req.attachments:
            entry = attachment_store.get(attachment_id)
            if entry is not None:
                attachments.append({
                    "attachment_id": entry.attachment_id,
                    "filename": entry.filename,
                    "content_type": entry.content_type,
                })

        def collect_panel(result: dict) -> None:
            panels.append({"kind": "invoice_run", **result})

        # Nova has been seen narrating a plausible-sounding invoice outcome
        # ("matched cleanly, see the panel") without ever calling
        # run_supplier_invoice_match - that leaves the LLM's own judgment as
        # the only thing standing between "nothing was extracted" and a
        # confident false answer. When the message plainly reads as a request
        # to process the one attached file, run the tool here instead of
        # leaving that call up to the model, so the extraction/match actually
        # happen for real rather than depending on Nova choosing to invoke it.
        invoice_result: dict | None = None
        if len(attachments) == 1 and wants_invoice_check(req.question):
            invoice_result = run_supplier_invoice_match(
                attachment_id=attachments[0]["attachment_id"],
                on_result=collect_panel,
                run_id=req.run_id,
            )
            invoice_result = {**invoice_result, "filename": attachments[0]["filename"]}

        user_prompt = build_mcp_user_prompt(
            req.question, req.history, settings.history_char_budget,
            attachments=attachments, invoice_result=invoice_result,
        )
        # The invoice tool exists for this turn only if a file is attached AND
        # it has not already been run above - once run, offering it again
        # would just invite the model to call it a second time.
        specs = build_tool_specs(has_attachment=bool(attachments) and invoice_result is None)

        try:
            result = provider.generate_with_tools(
                SAP_MCP_SYSTEM_PROMPT,
                user_prompt,
                specs,
                settings.generation_max_tokens,
                build_execute_tool(on_invoice_result=collect_panel, run_id=req.run_id),
            )
            answer = result.answer
        except GenerationError as exc:
            error = str(exc)
            log.warning("SAP MCP generation failed: %s", exc)

        # A file was attached but no invoice_run panel came back - either the
        # model legitimately left it alone (a question about something else)
        # or it skipped calling run_supplier_invoice_match while still
        # narrating an outcome in prose (seen with Nova: it answers "matched
        # cleanly, see the panel" without ever invoking the tool). Either way
        # the UI opened a live drawer the moment the turn was sent, so it must
        # get SOMETHING back to show instead of silently vanishing with no
        # trace and no way to reopen it - see the front-end's beginInvoiceRun.
        #
        # The chat bubble needs the same correction as the panel: `answer` is
        # still whatever Nova said, and Nova's own prose is exactly what's
        # unreliable here - the "matched cleanly, see the panel" narration
        # this branch exists for. Leaving it in place puts a fabricated
        # result right next to the panel's honest "didn't run" state, so the
        # text is overridden too rather than only patching the panel.
        if attachments and not panels and error is None:
            answer = (
                "I did not actually run the invoice check on "
                f"\"{attachments[0]['filename']}\" - nothing was extracted or matched, "
                "so anything I said above about its result was wrong. Please attach "
                "the file again and ask me to check/match it."
            )
            panels.append({
                "kind": "invoice_run",
                "ok": True,
                "outcome": "not_run",
                "run_id": req.run_id,
                "filename": attachments[0]["filename"],
            })

    # Reuses QueryResponse for a single frontend render path; retrieval-only
    # fields stay empty since this interface has no notes/chunks/KB.
    return QueryResponse(
        answer=answer,
        error=error,
        citations=[],
        chunks=[],
        provider=provider.name if provider else "bedrock",
        model=provider.model if provider else "-",
        knowledge_base="",
        latency_ms=int((time.perf_counter() - started) * 1000),
        panels=panels,
    )


@app.post("/api/attachments")
def upload_attachment(file: UploadFile = File(...)) -> dict:
    """Stage a file for the next chat turn and return only its id.

    The bytes stay in the server-side attachment store; the chat payload and the
    model's tool call carry the id alone. Nothing here inspects or runs anything -
    whether the file gets processed is the model's routing decision on the turn
    that references it."""
    data = file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload.")
    if len(data) > settings.attachment_max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File is {len(data) // 1024} KB; the limit is "
                   f"{settings.attachment_max_bytes // 1024} KB.",
        )
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in _ALLOWED_ATTACHMENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{content_type or 'unknown'}'. "
                   "Attach a JPEG, PNG or PDF.",
        )
    entry = attachment_store.put(file.filename or "attachment", content_type, data)
    return {
        "ok": True,
        "attachment_id": entry.attachment_id,
        "filename": entry.filename,
        "content_type": entry.content_type,
        "size": entry.size,
    }


# ---- Supplier Invoice matching agent -------------------------------------
# These endpoints only translate HTTP <-> the plain-Python tools in
# app/mcp/invoice_tools.py; no matching/posting logic lives here. A sync `def`
# runs in FastAPI's threadpool, so the blocking SAP/Bedrock calls inside the
# pipeline don't stall the event loop.


@app.post("/api/invoice/process")
def invoice_process(file: UploadFile = File(...)) -> dict:
    """STEP 1. Run extract -> dedup -> match on an uploaded invoice (image or
    PDF). Returns the run_id (poll the trace) plus the final structured
    outcome. A clean match ends at `matched` + a `match_id` and runs NO posting
    checks - posting is a separate request (/api/invoice/propose)."""
    data = file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload.")
    return run_invoice_pipeline(data, file.content_type or "")


@app.post("/api/invoice/propose/{match_id}")
def invoice_propose(match_id: str) -> dict:
    """STEP 2. The user has seen a finished 3-way match and wants to post it:
    run the posting pre-flight and build the proposal. Takes only a match_id -
    every figure comes from the stored match, none from the caller. Still
    writes nothing to SAP; approval (/commit) remains a separate click."""
    return propose_posting_for_match(match_id)


@app.get("/api/invoice/trace/{run_id}")
def invoice_trace(run_id: str, after: int = -1) -> dict:
    """Poll a pipeline run's trace events after sequence `after` (the live
    agent-trace panel)."""
    return trace_store.events_after(run_id, after)


@app.post("/api/invoice/commit/{proposal_id}")
def invoice_commit(proposal_id: str) -> dict:
    """Post a proposed, HUMAN-APPROVED supplier invoice to SAP. This deliberate
    HTTP action is the explicit approval boundary: the only writer fires here,
    never from a model turn or the pipeline itself."""
    return commit_post_supplier_invoice(proposal_id)


@app.post("/api/invoice/proposal/{proposal_id}/edit")
def invoice_proposal_edit(proposal_id: str, body: ProposalEdit) -> dict:
    """Apply human edits to a not-yet-approved proposal (same proposal_id,
    rebuilt payload). Never writes to SAP - only /commit does that."""
    return update_proposal_fields(proposal_id, body.edits)


@app.get("/api/invoice/tolerances")
def invoice_tolerances_get() -> dict:
    return get_tolerances()


@app.post("/api/invoice/tolerances")
def invoice_tolerances_set(update: ToleranceUpdate) -> dict:
    """Override matching tolerances at runtime - no restart (same spirit as the
    per-request KB toggle)."""
    return update_tolerances(**update.model_dump(exclude_none=True))


@app.get("/api/invoice/review")
def invoice_review() -> dict:
    """Open human-review items (low-confidence extractions the pipeline refused
    to guess on)."""
    return list_review_queue()


@app.post("/api/invoice/review/{review_id}/retry")
def invoice_review_retry(review_id: str, body: ReviewRetry) -> dict:
    """Retry a parked PO-match review item with a human-corrected PO number
    (po_not_found / po_line_not_found / ambiguous_po_line). Never writes to
    SAP itself - a successful retry lands on the same propose/approve gate as
    any other invoice."""
    return retry_match_with_corrected_po(review_id, body.corrected_po_number)


@app.get("/api/status", response_model=StatusResponse)
def status() -> StatusResponse:
    store: VectorStore = app.state.store
    ledger = IngestionLedger(settings.ledger_path)
    provider = app.state.provider
    mcp_provider = app.state.mcp_provider
    return StatusResponse(
        ok=provider is not None,
        provider=provider.name if provider else f"{settings.generation_provider} (unconfigured)",
        model=provider.model if provider else "-",
        embedding_model=settings.embedding_model,
        vector_store=f"qdrant ({store.mode})",
        collection=settings.qdrant_collection,
        indexed_chunks=store.count(),
        indexed_documents=len(ledger),
        knowledge_bases={"local": True, "bedrock": "bedrock" in app.state.retrievers},
        default_knowledge_base=settings.knowledge_base,
        mcp_ok=mcp_provider is not None,
        mcp_provider=mcp_provider.name if mcp_provider else "bedrock (unconfigured)",
        mcp_model=mcp_provider.model if mcp_provider else "-",
        # Asks the credential seam, not SAP_BASE_URL: under the shipped default
        # the base URL lives in Secrets Manager and the .env value is empty.
        sap_configured=sap_credentials_configured(settings),
    )


@app.post("/api/ingest", response_model=IngestResponse)
def ingest(rebuild: bool = False) -> IngestResponse:
    source = LocalFolderSource(settings.notes_dir)
    try:
        report = run_ingestion(source, app.state.embedder, app.state.store, rebuild=rebuild)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {exc}") from exc
    return IngestResponse(**report.as_dict())


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

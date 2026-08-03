# SAP Helper — RAG Chatbot POC for SAP Note Retrieval

A proof-of-concept backend (plus a small chat UI) that takes a natural-language
description of an SAP problem, retrieves the most relevant SAP Notes from a
vector index, and synthesizes troubleshooting guidance **grounded strictly in
those notes**, with clickable note citations.

Built as a tender deliverable: it demonstrates retrieval quality and an
architecture that scales to ~2M documents, without pretending to be
production-hardened.

```
PDFs ──▶ parse (Symptom/Cause/Resolution) ──▶ structure-aware chunks
     ──▶ local embeddings (BGE, self-hosted) ──▶ Qdrant (int8 quantization)
                                                        │
user question ──▶ embed ──▶ top-k search ──▶ citations ─┤
                                                        ▼
                             Bedrock ◀── one generation interface ──▶ Groq
                                          (chosen by config)
```

## Layout

| Path | Responsibility |
|---|---|
| `app/ingestion/` | Document source abstraction, SAP Note parser, structure-aware chunker, incremental-ingestion ledger, pipeline |
| `app/retrieval/` | Embedding abstraction (fastembed/BGE), Qdrant store with scalar quantization, local retriever + citation builder, Bedrock Knowledge Base retriever |
| `app/generation/` | `GenerationProvider` interface, Bedrock adapter, Groq adapter, shared grounding prompt, factory |
| `app/mcp/` | MCP server for SAP transactions: master-data lookup, Purchase Order preview→confirm→create with CSRF handshake and idempotency ledger, PO status, PO↔Goods Receipt 2-way match (also reachable from the main chat UI - see below) ([docs](app/mcp/README.md)) |
| `app/main.py` | FastAPI app: `POST /api/query`, `GET /api/status`, `POST /api/ingest`, serves the UI |
| `scripts/` | `ingest.py` CLI, `make_sample_notes.py` (synthetic demo PDFs), `eval/` (agent evaluation — see [Testing](#testing)) |
| `tests/` | Layer 1 (offline seam + unit tests) and Layer 2 (live read-only tenant contract tests) |
| `static/index.html` | Chat UI (no build step) |

## Quick start

Requires Python 3.11+ (developed and tested on 3.14).

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# 1. Get some PDFs. Either drop real SAP Note PDFs into .\data\notes,
#    or generate the bundled synthetic demo set:
.\.venv\Scripts\python -m scripts.make_sample_notes

# 2. Configure a generation backend
copy .env.example .env     # then set AWS credentials (or switch to groq, below)

# 3. Ingest (first run downloads the ~130 MB embedding model once)
.\.venv\Scripts\python -m scripts.ingest

# 4. Run
.\.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

Open <http://127.0.0.1:8000> for the chat UI, or call the API directly:

```bash
curl -X POST http://127.0.0.1:8000/api/query \
  -H "Content-Type: application/json" \
  -d '{"question": "HANA indexserver crashes with OOM during delta merge"}'
```

The response contains `answer` (with inline `[Note 1234567]` citations),
`citations[]` (note ID, title, clickable URL, matched sections, score) and the
raw retrieved `chunks[]`.

Without any generation credentials the server still starts and `/api/query`
returns retrieval results plus a clear error instead of an answer — useful for
demoing the retrieval core in isolation.

## Pointing it at your own PDFs

Set `NOTES_DIR` in `.env` (default `./data/notes`) and re-run
`python -m scripts.ingest`. Ingestion is **incremental**: a JSON ledger
(`data/ingestion_ledger.json`) records the content hash of each processed
file, so unchanged files are skipped and changed files replace their old
vectors. `--rebuild` wipes the collection and ledger and starts fresh.

> With the default embedded Qdrant, the database is single-process. Stop the
> API server before running the CLI ingest, or use `POST /api/ingest`
> (optionally `?rebuild=true`) against the running server instead.

## Switching between Bedrock and Groq

One config value; no code changes. Both adapters implement the same
`GenerationProvider` interface and share the same grounding prompt.

**Amazon Bedrock** (default; model: Amazon Nova Pro):

```dotenv
GENERATION_PROVIDER=bedrock
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=us.amazon.nova-pro-v1:0
```

Bedrock uses standard AWS credential resolution (env vars, `~/.aws`, SSO, or
instance role) via boto3's model-agnostic Converse API, so any Bedrock chat
model works — Nova Lite/Micro (`us.amazon.nova-lite-v1:0`,
`us.amazon.nova-micro-v1:0`), Anthropic Claude, Meta Llama, etc. Nova model
ids need the cross-region inference profile prefix (`us.`). Make sure the
chosen model is enabled for your account in the Bedrock console.

**Groq**:

```dotenv
GENERATION_PROVIDER=groq
GROQ_API_KEY=gsk_...
GROQ_MODEL=llama-3.3-70b-versatile
```

Adding a third provider = one new adapter file implementing
`GenerationProvider.generate()` plus one branch in
`app/generation/factory.py`.

## Switching the knowledge base: local Qdrant vs. Amazon Bedrock Knowledge Base

The chat UI header has a **Local KB / Bedrock KB** toggle that switches which
knowledge base the LLM retrieves context from, per-request — no restart
needed. Both retrieval backends implement the same `retrieve(question, top_k)
-> (chunks, citations)` contract, so the generation layer (Bedrock or Groq,
whichever `GENERATION_PROVIDER` you've configured) never knows which one
answered.

- **Local KB** (`app/retrieval/retriever.py`) — the self-hosted Qdrant +
  fastembed pipeline described above. Always available.
- **Bedrock KB** (`app/retrieval/bedrock_kb.py`) — calls the
  `bedrock-agent-runtime` `Retrieve` API against an existing Amazon Bedrock
  Knowledge Base (its own S3/OpenSearch-backed vector index, managed
  entirely in AWS — not the local Qdrant store). Enable it:

  ```dotenv
  BEDROCK_KB_ID=<your knowledge base id>
  KNOWLEDGE_BASE=bedrock   # optional: makes it the startup default
  ```

  Uses the same AWS credential resolution and `AWS_REGION` as the Bedrock
  generation provider. If `BEDROCK_KB_ID` is unset, the toggle option is
  grayed out in the UI and `/api/status` reports it unavailable; picking it
  via the API returns a clear per-request error instead of failing startup.

`GET /api/status` reports `knowledge_bases: {local, bedrock}` (availability)
and `default_knowledge_base`; `POST /api/query` accepts an optional
`knowledge_base: "local" | "bedrock"` field (defaults to the server setting)
and echoes back which one it used.

## The 2M-document scaling story

The POC ingests a handful of PDFs, but nothing in the pipeline assumes small
scale:

- **Vector store** — Qdrant with **int8 scalar quantization** configured at
  collection creation (~4× less vector RAM; original vectors stay on disk for
  rescoring). 2M notes ≈ 10M chunks at 384 dims ≈ ~15 GB quantized — a single
  reasonable node, before sharding. Setting `QDRANT_URL` switches from the
  embedded demo database to any Qdrant server/cluster with zero code changes;
  sharding, replication and the quantized HNSW path all live server-side.
  (Embedded mode accepts the quantization config but only the server actually
  exercises it — that's a property of the demo database, not of the code.)
- **Document access** — ingestion consumes a `DocumentSource` interface, not
  file paths. Swapping "local folder" for "client portal API" is one new
  subclass yielding the same `(doc_id, display_name, content_hash, bytes)`
  records; parsing, chunking, embedding and storage are untouched.
- **Streaming ingestion** — documents are processed one at a time and upserted
  in batches with deterministic point IDs (idempotent re-ingest). Scaling
  throughput means parallelizing the loop, not rewriting it.
- **Incremental ingestion** — the content-hash ledger means a nightly re-crawl
  of 2M documents only re-embeds what changed.
- **Self-hosted embeddings** — BGE runs locally via ONNX; there is no
  per-token embedding API cost standing between you and 10M chunks.

## Design choices (where the spec was open)

| Choice | What / why |
|---|---|
| Embedding model | `BAAI/bge-small-en-v1.5` (384 dims, MIT license) via **fastembed** (ONNX runtime — no PyTorch, ~50 MB of runtime deps instead of ~2 GB). Strong retrieval quality for its size; query-side instruction prefix handled by the embedder. Swappable via `EMBEDDING_MODEL` / a new `Embedder` subclass. |
| Vector DB | **Qdrant** — open-source, self-hostable, first-class scalar/binary quantization, filtered search for later metadata facets (component, product). Embedded mode keeps the POC zero-infrastructure. |
| Chunking | One chunk per SAP Note **section** (Symptom, Cause, Resolution, …), each prefixed with the note ID + title so no chunk loses its parent context; oversized sections split on paragraph boundaries with overlap (`MAX_CHUNK_CHARS=1800`). A symptom match always cites the note whose Resolution the generator can also see. |
| Parsing | `pypdf` text extraction + heading-based section splitting tuned to the SAP Note layout; note ID / title / component pulled from the header. Unrecognized layouts degrade gracefully to a single "Overview" section rather than failing. |
| Note links | `SAP_NOTE_URL_TEMPLATE` config (default `https://me.sap.com/notes/{note_id}`) — the client portal can substitute its own pattern. |
| Grounding | The system prompt (shared across providers, `app/generation/prompts.py`) forbids outside knowledge, requires `[Note <id>]` citations for every claim, and mandates an explicit "not found in the corpus" answer instead of guessing. |
| Retrieval | Pure vector similarity (per spec). BM25/hybrid and reranking are deliberate non-goals; the retriever is the single place they'd slot in. |
| Failure mode | If generation fails (bad key, quota), the API still returns citations + the error, so retrieval remains demonstrable. |

## Creating SAP transactions (MCP server)

Separate from the read-only RAG flow, `app/mcp/` ships an MCP server that
lets an LLM create **Purchase Orders** in a live S/4HANA Cloud tenant from
natural language — free-text supplier/material/plant references are resolved
to ranked master-data candidates, every order is previewed for explicit user
confirmation before the single write-capable tool runs, and an idempotency
ledger prevents duplicate documents on retries. Runs as its own process:

```powershell
.\.venv\Scripts\python -m app.mcp.server   # stdio; HTTP via SAP_MCP_TRANSPORT
```

Configuration (credentials, org-structure defaults), the CSRF handshake, tenant
assumptions to verify, and the Supplier Invoice 3-way match built on top of this
layer are documented in [`app/mcp/README.md`](app/mcp/README.md).

### PO ↔ Goods Receipt check, from the main chat UI

On tenants where only PO and Goods Receipt read access is authorized (no
master-data or write access yet), the chat UI can still demo a live SAP
check without the MCP server running: mention a PO number (a bare 10-digit
number, e.g. `4500000002`) in a chat message and `/api/query` short-circuits
the RAG pipeline, calls the same PO↔GR comparison logic used by the MCP
tool `compare_po_to_goods_receipts`, and answers with ordered-vs-received
quantities per item instead of a notes-grounded answer. This path is a 2-way
match by design (PO vs. receipts, no invoice leg) — the third leg lives in the
Invoice Agent mode below, which starts from an invoice document rather than a PO
number.

### Supplier invoice 3-way match — attach a file in the SAP MCP chat

The invoice agent lives inside the SAP MCP chat rather than on its own screen.
Press **+** in the composer, attach a photographed or PDF supplier invoice, and
type what you want — the model then decides whether to run the invoice agent or
answer the SAP question you actually asked. That decision is bounded, not hoped
for: the invoice tool is only added to the model's tool list when the turn
carries an attachment.

When it does run, it performs the full **PO ↔ Goods Receipt ↔ Invoice** match:
Nova vision reads the fields with a confidence per field, a duplicate check runs
before anything else, the PO line is resolved (by item number, or a flagged
material-description fallback), goods receipts are summed net of reversals, and
plain-Python arithmetic compares the three against configurable tolerances. A
mismatch produces a structured variance record, an LLM-written explanation whose
numbers are all rendered server-side, and a numeral-guarded SES email.

**A clean match ends there** — matching an invoice and posting it are two
separate decisions, so they are two separate steps. "Does this agree with the PO
and the receipt?" is answered without ever asking "will SAP accept a posting?",
which is a question about master data on the PO and would otherwise bury a
perfect match under blockers nobody asked about. Getting it into SAP takes two
deliberate clicks: **Continue to posting** (runs SAP's pre-flight checks and
builds the proposal, writing nothing) and then **Approve & post** — the only
thing that writes to SAP.

The reply carries a **clickable result card**; opening it slides in a right-hand
panel (narrowing the chat, collapsing the sidebar) with the live agent trace,
the extracted fields and confidences, the computed figures, and those buttons. The trace streams while the turn is still in flight — that's the visible
agentic behaviour of the demo. The model's own reply is deliberately number-free;
every figure lives in the panel, rendered server-side.

Tolerance defaults and rationale, the extraction prompt, the Secrets Manager
secret shape, SES sandbox setup, and the Supplier Invoice API fields worth
flagging are all documented in
[`app/mcp/README.md`](app/mcp/README.md#supplier-invoice-matching-agent-the-3-way-match).

```powershell
# Find two real POs to build the demo's matching + mismatching invoices from:
.\.venv\Scripts\python -m scripts.find_demo_invoices --max 40
```

Credentials for the shipped build come from **AWS Secrets Manager**, not `.env`;
set `LOCAL_DEV=true` to fall back to the `.env` `SAP_*` values for local
iteration.

## Sample data

Real SAP Notes are SAP-proprietary, so the repo generates **synthetic**
note-shaped PDFs (`scripts/make_sample_notes.py`) — eight invented but
plausible Basis-flavored notes with the real layout (header line, Symptom /
Environment / Cause / Resolution / Keywords, component). Note numbers and
content are fabricated for demo purposes only.

## Testing

"Test" means three different things in a system like this, and they need
different machinery. All three run from the project root.

| Layer | Answers | Network? | Runtime |
|---|---|---|---|
| **1 — Seam + unit** | Is the plumbing connected? Is the arithmetic right? | No | ~2 s |
| **2 — Live contract** | Do the OData calls actually work on this tenant? | Yes (read-only) | ~30 s |
| **3 — Agent eval** | Does Nova route to the right tool and narrate without inventing? | Bedrock only | minutes |

### Layer 1 — offline suite

```powershell
.\.venv\Scripts\python -m pytest
```

Never touches the network. Two kinds of test:

*Unit tests* over the pure modules — `compare_po_to_receipts`, the three
analysis reports, `match_invoice`, `find_po_line`. These modules already take
their dependencies as arguments (`build_po_aging_report` receives `today`
rather than reading the clock), so they need no mocking and the assertions are
exact.

*Seam contract tests* (`tests/test_seams.py`) — the ones that matter most here.
Every gap found in the 2026-07-26 and 2026-07-27 audits had one shape: two
halves of a feature written to different assumptions, each correct alone, so
nothing raised and no test failed — the feature was simply unreachable. Unit
tests cannot catch that, because each half passes its own. These assert the
contract *between* halves: the key one side writes is the key the other reads,
the branch below a gate is reachable given that gate, the enum one side emits
is in the table the other looks up.

They are verified by mutation: each historical bug was reintroduced and the
matching test confirmed to fail (6/6 caught). A test that cannot fail is
decoration.

`test_seams.py` also pins the safety invariants — no write-capable tool may
appear in any chat tool spec, the invoice tool exists only when a file is
attached, and `tests/test_live_sap.py` stays read-only. That last guard lives
in the always-on suite deliberately: a guard that only runs once you have asked
for `-m live` fires after the requests it was meant to prevent.

### Layer 2 — live tenant contract tests

```powershell
.\.venv\Scripts\python -m pytest -m live
```

Deselected by any ordinary run, so `pytest` alone is always offline and safe.
Skips rather than fails when credentials are absent — a laptop without tenant
access should report "not verified", not a red suite.

Asserts **shape, never values**. A live tenant's data changes; a test claiming
"PO 4500000261 has quantity 47" fails the first time somebody posts a goods
receipt and teaches the team to ignore the file.

It exists mainly for one question: the bulk `PurchaseOrder` read with an OData
v4 `$filter` and an item `$expand` has only ever run against synthetic data,
and all three analysis tools stand on it. If it 400s, three tools advertised to
the chat model fail on their first real question.

Prints a dated verification matrix at the end — the artifact this layer is for:

```
CHECK                                  RESULT        ms  DETAIL
PO service reachable ($metadata)       PASS         412  purchaseorder
bulk PO query ($filter + $expand)      PASS         1204 5 PO(s) returned
master data: supplier                  PASS         380  still blocked (HTTP 403) - expected
```

Master-data checks **record** their status rather than asserting success: those
services are known-403 on this tenant, so the useful output is a dated answer
to "is it still blocked?", not a permanent red mark.

### Layer 3 — agent evaluation

```powershell
.\.venv\Scripts\python -m scripts.eval --dry-run      # harness self-check, no cost
.\.venv\Scripts\python -m scripts.eval                # real run against Nova
.\.venv\Scripts\python -m scripts.eval --baseline     # store data/eval_baseline.json
.\.venv\Scripts\python -m scripts.eval --compare      # diff against that baseline
```

30 cases stratified simple / medium / complex / adversarial, each run 3× by
default. The SAP tools are stubbed with recorded fixtures; everything else —
system prompt, tool specs, prompt builder, Converse tool-use loop — is
production code, imported not copied. The stub is the point: this eval asks
whether *the model* behaves, not whether SAP is up. Layer 2 answers that, and
mixing them yields a score that moves for reasons you cannot attribute.

Six dimensions, all deterministic — no LLM judge:

| Dimension | Weight | Floor | What it checks |
|---|---|---|---|
| routing | 0.30 | 0.85 | The expected tool ran (or correctly none did) |
| number_fidelity | 0.25 | 0.95 | Every figure in the answer traces to a tool result |
| document_fidelity | 0.15 | 0.95 | SAP document numbers were not invented |
| refusal | 0.15 | 0.90 | Knowledge questions are declined and redirected |
| language | 0.10 | 0.90 | Reply language matches the question's |
| efficiency | 0.05 | 0.80 | Tool calls stay within budget |

**Number fidelity is the notable one.** Most systems cannot test for
hallucination deterministically; this architecture can, because the panel owns
every figure and the tool result carries the rest. So "does any number in this
prose fail to appear in the tool result?" is a real detector rather than a
proxy. Derived arithmetic counts as ungrounded on purpose — a model computing
"80% received" is doing exactly the non-deterministic arithmetic
`app/mcp/analysis.py` was written to take away from it, even when the sum is
right.

**Language fidelity** turns the known drift bug (the same English question
answered in Portuguese or Spanish) from "sometimes it goes weird" into a rate
that can be re-measured after a fix.

**Repeats are not optional.** Nova is non-deterministic, so a single green run
is not evidence — and the failures this project has actually hit (a skipped
tool call, a drifted language) are intermittent by nature and invisible to
one-shot manual testing. Repeats turn them into a pass *rate* plus an explicit
flaky list.

**Per-dimension floors** gate alongside the weighted average, and the run exits
non-zero if either fails. An agent that routes flawlessly and fabricates every
figure still averages well above threshold, because routing carries twice
number fidelity's weight; the floors are what surface that.

Two limits stated plainly rather than papered over: 30 cases is below the ~50
that gives tight confidence intervals, so small movements between runs are
noise; and the runner deliberately skips production's `wants_invoice_check()`
regex short-circuit, so the invoice cases measure the model's own routing
rather than the safety net in front of it — which is the number worth having,
since the net exists precisely because that routing is unreliable.

## Out of scope (per the brief)

No auth, no real portal integration (abstraction in place), no load testing at
2M scale, no fine-tuning / reranker / hybrid search.

Not covered by any test layer: the Bedrock vision extraction step
(`invoice_extraction.py`) is exercised only through stubbed providers — scoring
OCR field accuracy needs a labelled corpus of real invoices, which does not
exist yet. The RAG interface (`POST /api/query`) has no agent eval of its own;
Layer 3 covers the SAP chat only.

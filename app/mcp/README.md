# SAP Transactions — MCP Server

An MCP server that lets the chatbot's LLM create **Purchase Orders** in a live
S/4HANA Cloud tenant from natural language, with a hard two-step safety
boundary between *proposing* a document and *creating* it.

```
user: "order 20 hex bolts from the steel supplier for the main plant"
        │
        ▼
 search_master_data ──── (optional exploration, read-only)
        │
        ▼
 preview_purchase_order ── resolves names → codes, returns summary + preview_id
        │                  (ambiguous name? → candidates back to the user)
        ▼
   user confirms the summary  ◀── the LLM must show it verbatim
        │
        ▼
 create_purchase_order(preview_id) ── CSRF handshake → OData POST → PO number
        │                             (idempotency ledger: at most one PO
        ▼                              per confirmed preview, even on retries)
 get_purchase_order_status ── read-only verification
```

## Tools

| Tool | Writes? | Purpose |
|---|---|---|
| `sap-transactions:search_master_data` | no | Free text → ranked supplier/material/plant candidates |
| `sap-transactions:preview_purchase_order` | no | Resolve a full PO request; mint a single-use `preview_id` |
| `sap-transactions:create_purchase_order` | **yes** | Execute an already-confirmed preview — takes *only* a `preview_id` |
| `sap-transactions:get_purchase_order_status` | no | PO header/items/processing status by document number |
| `sap-transactions:compare_po_to_goods_receipts` | no | PO vs. Goods Receipt 2-way match by document number (see below) |

### PO ↔ Goods Receipt 2-way match

Added for tenants where master-data lookup (supplier/material/plant) isn't
authorized yet, so `preview_purchase_order`/`create_purchase_order` can't run,
but PO read (`API_PURCHASE_ORDER_2`) and Goods Receipt read
(`API_MATERIAL_DOCUMENT_SRV`, `SAP_GR_SERVICE`) are. Given a PO number, it
pulls the PO's ordered quantities and every Material Document item that
references that PO, nets receipts against reversals (`DebitCreditCode`: `S`
receipt / `H` reversal), and reports per item whether the full quantity was
received. **This is a 2-way match, not the full 3-way match** — the third leg
(Supplier Invoice) needs its own service; see "Known POC limits" below.
`app/mcp/gr.py` holds the I/O, `compare_po_to_receipts` in `po.py` is the pure
comparison logic, and it's also wired directly into the main chat app's
`/api/query` (`app/main.py`) - a bare 10-digit number in the question
short-circuits the RAG pipeline and runs this check instead.

## Implementation choices (and why)

- **MCP SDK**: the official `mcp` Python package (`mcp.server.fastmcp.FastMCP`),
  the reference implementation — decorator-based tools whose docstrings/type
  hints become the tool schema, and it supports both transports below without
  code changes.
- **Process model**: the server runs as its **own process**
  (`python -m app.mcp.server`), not mounted into the FastAPI app. The chat/UI
  integration is being built separately; a standalone server works today with
  any MCP client (Claude Desktop, Claude Code, an agent SDK) over `stdio`, and
  flipping `SAP_MCP_TRANSPORT=streamable-http` serves `/mcp` on
  `SAP_MCP_HOST:SAP_MCP_PORT` for an HTTP-based chat layer. It reuses
  `app.config.settings`, so one `.env` drives both processes.
- **Tool naming**: verb-noun, one tool per unambiguous purpose. Lookup is one
  consolidated tool with an `entity_type` parameter rather than three
  near-identical tools, so the model never has to choose between overlapping
  descriptions.
- **Preview/create split**: two genuinely separate tools. `create_purchase_order`
  accepts **only a `preview_id`** — not field values, not a payload, and not a
  `confirm=true` flag — so there is no code path where the model executes
  something it didn't preview. Preview entries are single-use, expire after
  `SAP_PREVIEW_TTL_SECONDS` (default 30 min), and live only in server memory:
  a restart invalidates proposals (safe), while *executed* documents are
  persisted in the idempotency ledger (see below).
- **Idempotency**: before POSTing, the create tool checks
  `data/po_idempotency_ledger.json` (same JSON-ledger pattern as
  `app/ingestion/ledger.py`), keyed on `sha256(payload_hash + preview_id)`.
  A retried or duplicated create for the same confirmed preview returns the
  PO number SAP already assigned instead of creating a twin — including across
  server restarts. An intentional second identical order gets a fresh preview,
  and the preview itself warns that a twin document exists.
- **Error translation**: SAP Gateway errors (verbose JSON or XML) are parsed
  down to the actual message text (`error/message/value` +
  `innererror/errordetails`) and returned as one plain sentence; the raw
  payload goes to the server log (stderr) only. Tools return
  `{ok: false, error, ...}` structures rather than raising, so the calling LLM
  can recover (re-ask the user, re-preview) instead of surfacing a protocol
  error.

## The CSRF handshake

SAP OData **reads** need only Basic Auth. **Writes** require a token dance,
implemented once in `client.py` (`SAPClient.post`), never per-tool:

1. `GET <service-root>/` with header `X-CSRF-Token: Fetch` (Basic Auth).
2. SAP responds with an `x-csrf-token` header **and** session cookies
   (`SAP_SESSIONID_*`). The token is only valid together with those cookies.
3. `POST` the payload with `X-CSRF-Token: <token>`; the cookies ride along
   automatically in the client's cookie jar.
4. If SAP answers `403` with `x-csrf-token: Required` (session expired), the
   client re-fetches once and retries the POST.

Tokens are cached per service root for the life of the process. Token and
cookies exist **only in memory** — they are never written to disk and never
logged (raw error *bodies* are logged for debugging; auth headers and cookies
are not).

Auth itself is isolated behind `SAPClient._build_auth()`. Swapping Basic for
OAuth 2.0 (client credentials against `SAP_OAUTH_TOKEN_URL`) is one new branch
in that method — no tool or resolver changes. The config fields already exist.

## Configuring a tenant

Copy `.env.example` → `.env` (shared with the RAG app) and set:

```dotenv
SAP_BASE_URL=https://myXXXXXX-api.s4hana.cloud.sap   # the -api host, not the UI host
SAP_USERNAME=<communication user>
SAP_PASSWORD=<password>

# Org defaults applied when the user doesn't specify them:
SAP_DEFAULT_COMPANY_CODE=1710
SAP_DEFAULT_PURCHASING_ORG=1710
SAP_DEFAULT_PURCHASING_GROUP=001
SAP_DEFAULT_ORDER_TYPE=NB
SAP_DEFAULT_CURRENCY=USD
```

The communication user needs communication arrangements for:

| Service | Used for | Default path |
|---|---|---|
| `API_PURCHASE_ORDER_2` (OData **v4**) | PO create + status | `/sap/opu/odata4/sap/api_purchaseorder_2/srvd_a2x/sap/purchaseorder/0001` |
| `API_BUSINESS_PARTNER` (SAP_COM_0008) | supplier lookup | `/sap/opu/odata/sap/API_BUSINESS_PARTNER` |
| `API_PRODUCT_SRV` (SAP_COM_0009) | material lookup | `/sap/opu/odata/sap/API_PRODUCT_SRV` |
| Plant (OData **v4**, SAP_COM_0839) | plant lookup | `/sap/opu/odata4/sap/api_plant/srvd_a2x/sap/plant/0001` |

Paths are configurable (`SAP_PO_SERVICE`, `SAP_SUPPLIER_SERVICE`,
`SAP_PRODUCT_SERVICE`, `SAP_PLANT_SERVICE`) because tenants differ by release.
The resolver knows the v2 vs v4 filter dialects (`substringof` vs `contains`);
each lookup is a `LookupSpec` record in `resolver.py`, so re-pointing a lookup
at a different service/entity is data, not code.

> **If your tenant only released the older v2 `API_PURCHASEORDER_PROCESS_SRV`**
> instead of `API_PURCHASE_ORDER_2`, the PO module needs its v4-specific names
> swapped back to v2 ones: entity set `A_PurchaseOrder` (not `PurchaseOrder`)
> and navigation property `to_PurchaseOrderItem` (not `_PurchaseOrderItem`) in
> both `server.py` and `po.py`.

## ⚠ Tenant assumptions to verify

Checked against the SAP Best Practices **US model company (1710)**. Before
relying on this against your tenant, verify via each service's `$metadata`
and your customizing:

- **PO item navigation property**: `_PurchaseOrderItem` is SAP's documented
  v4 naming convention for `API_PURCHASE_ORDER_2`, but this codebase hasn't
  been verified against a live tenant's actual `$metadata` yet. If the first
  `create_purchase_order` call 400s complaining about an unknown property
  (rather than a data/authorization error), fetch `<SAP_PO_SERVICE>/$metadata`
  in a browser, find the real association name from `PurchaseOrder` to
  `PurchaseOrderItem`, and use that instead in `po.py` (`build_odata_payload`,
  `parse_po_status`) and `server.py` (`$expand`).
- **Org codes**: company code / purchasing org `1710`, purchasing group `001`,
  order type `NB`, currency `USD` are demo-tenant values. Real tenants differ.
- **Mandatory PO fields**: the payload sends header
  `CompanyCode, PurchaseOrderType, Supplier, PurchasingOrganization,
  PurchasingGroup, DocumentCurrency` and item
  `Material, Plant, OrderQuantity` (+ optional unit/price). Tenants with
  extended checks (account assignment mandatory, release strategies, custom
  fields) will reject this minimal payload — the SAP error is surfaced
  verbatim enough to tell you which field.
- **Pricing**: items without an explicit price require a **purchasing info
  record** for the material/supplier/org combination, or creation fails.
- **Lookup fields**: supplier search uses `A_Supplier(Supplier, SupplierName)`;
  material search uses `A_ProductDescription` filtered to
  `SAP_MATERIAL_SEARCH_LANGUAGE` (default `EN`); plant search uses
  `Plant(Plant, PlantName)` on the v4 service. Field names shift between
  releases — check `$metadata` if lookups 400.
- **`tolower()` search**: case-insensitive filters aren't enabled on every
  gateway; the resolver automatically retries case-sensitively on a 400.
- **Status codes**: `PurchasingProcessingStatus` values are mapped for common
  cases (held/ordered/…); unmapped codes are returned raw in `status_code`.
- **One plant per PO**: the POC applies one receiving plant to all items
  (per-item plants are a payload-only extension in `po.py`).

## Running it

```powershell
# stdio (default) - for Claude Desktop / Claude Code / SDK-spawned clients:
.\.venv\Scripts\python -m app.mcp.server
```

Claude Code registration, for example:

```powershell
claude mcp add sap-transactions -- .\.venv\Scripts\python.exe -m app.mcp.server
```

Or as HTTP for a networked chat layer: set `SAP_MCP_TRANSPORT=streamable-http`
and point the client at `http://127.0.0.1:8001/mcp`.

## Testing preview → create manually

With an MCP client attached (or `mcp dev`/Inspector), walk the happy path:

1. `search_master_data(entity_type="supplier", description="domestic")` —
   expect ranked candidates; verifies connectivity + Basic Auth (read path).
2. `preview_purchase_order(supplier="<something ambiguous>", ...)` — expect
   `ok: false` with `needs_clarification` candidates, **no** preview_id.
3. `preview_purchase_order(supplier="17300001", items=[{"material": "TG11",
   "quantity": 10}], plant="1710")` — expect `ok: true`, a readable summary,
   a `preview_id`. Nothing exists in SAP yet (check Manage Purchase Orders).
4. `create_purchase_order(preview_id=...)` — expect a `45xxxxxxxx` number.
   This exercises the CSRF handshake; watch stderr for the POST log line.
5. Call `create_purchase_order` again with the **same** preview_id — expect
   `duplicate_suppressed: true` and the same PO number, no second document.
6. `get_purchase_order_status(purchase_order="45xxxxxxxx")` — expect items +
   plain-language status.
7. Negative checks: a made-up `preview_id` (actionable error, no write); wait
   past the TTL (expiry message); restart the server and retry step 4's id
   (ledger still suppresses the duplicate).

## Known POC limits

- **Timeout ambiguity**: if the create POST times out *after* SAP committed,
  the ledger has no entry and a retry could duplicate. The error message tells
  the model to check before retrying; production would confirm via a status
  query keyed on a client reference before any retry.
- Basic Auth via `.env`; no secrets manager. OAuth is the documented swap.
- The preview store is per-process; running multiple server instances would
  need a shared store.
- No `$batch`, no change operations (update/cancel PO), single-plant POs.

---

# Supplier Invoice Matching Agent (the 3-way match)

Extends this layer with the third leg — **PO ↔ Goods Receipt ↔ Supplier
Invoice** — driven from a photographed/PDF invoice. Built against the AP
**Supplier Invoice** service `API_SUPPLIERINVOICE_PROCESS_SRV` (OData v2,
SAP_COM_0057), verified against the tenant's live `$metadata` (see "Fields
flagged" below). It reuses everything above: `SAPClient` + CSRF, the
propose→commit boundary, the JSON-ledger pattern, and `compare_po_to_receipts`
for the received-quantity leg (no second GR summation was written).

```
 invoice image / PDF
        │
        ▼
 extract (Nova vision) ── per-field value + confidence
        │                 blocking field < threshold → human REVIEW QUEUE (no guess)
        │                 po_item < threshold → description fallback, or review
        ▼
 duplicate check ──────── vendor TEXT + vendor-invoice-no (ledger, pre-lookup)
        │                 hit → STOP, surface the existing document
        ▼
 PO line lookup ───────── by item number, or material-description FALLBACK (flagged)
        │                 not found / ambiguous → surface candidates AND queue for
        │                 a human; never guess which line
        ▼
 duplicate re-check ───── supplier CODE + reference: ledger, then live SAP
        │                 hit → STOP, surface the existing document
        ▼
 GR sum (compare_po_to_receipts) ── net of reversals
        │
        ▼
 match (pure Python) ──── fixed variance taxonomy, config tolerances
        │
   ┌────┴─────────────────────────────┐
   ▼ clean                            ▼ variance
 STEP 1 ENDS: "matched"          structured record → LLM writes prose →
 (match parked, no posting        numeral-guarded → SES (sandbox) email
  check has run)
        │
        │  ← a human clicks "Continue to posting". Only now:
        ▼
 propose_posting_for_match ── TIER 1 pre-flight → blocked (+ review queue)
        │                              or a proposal
        ▼
 human approves → commit_post_supplier_invoice (the ONLY writer)
```

## Matching and posting are two steps, on purpose

"Does this invoice match?" and "will SAP accept a posting?" are different
questions with different failure reasons, and a user uploading a document has
only asked the first. The pre-flight checks below judge **master data on the
PO** — payment terms, the GR-based-IV flag, deletion/release/final-invoice
indicators. Running them on every match meant a perfect 3-way match could come
back buried under a wall of blockers about things the invoice was never wrong
about, and get parked in the human review queue, when the user just wanted to
know whether the figures agreed.

So step 1 stops at the match and hands back a `match_id`; step 2
(`propose_posting_for_match`, `POST /api/invoice/propose/{match_id}`) runs the
pre-flight only when someone actually asks to post. Nothing was weakened: the
same checks run before the same payload is built, and a blocked posting is
still parked for a human — just at the point where a person has actually asked
for a posting, so there is real work for a named person to do.

The match record stays server-side (`invoice_matches.py`) and step 2 takes only
the opaque id, exactly as `commit` takes only a `proposal_id`: the figures a
posting is built from are always the ones the matcher computed, never anything
a caller supplied. One match yields at most one live proposal, since the commit
ledger's idempotency key is `(payload_hash, proposal_id)` and two proposals
would not dedup against each other.

Every step emits a structured **trace event** (`invoice_trace.py`) the UI polls
at `GET /api/invoice/trace/{run_id}` — the visible agentic behaviour.

## In the chat: attach an invoice, the model routes it

There is no separate invoice screen. The agent lives inside the **SAP MCP** chat,
which is where the live-tenant tools already are, so one conversation covers
"what's the status of PO X" and "check this invoice" without the user choosing an
interface first.

**The `+` button** in the composer (SAP MCP mode only) uploads a JPEG/PNG/PDF to
`POST /api/attachments`, which returns an id and keeps the bytes server-side. A
chip appears above the composer; the turn then carries **ids only**.

**The model decides.** With an attachment on the turn, `build_tool_specs()` adds
one extra tool — `run_supplier_invoice_match` — to the same read-only list. The
user still types a prompt, and the model picks:

| Turn | What happens |
|---|---|
| "check this invoice against its PO" + file | calls `run_supplier_invoice_match` |
| "status of PO 4500000002?" + file | calls `get_purchase_order_status`, says it left the file alone |
| "status of PO 4500000002?" (no file) | the invoice tool **does not exist** for that turn |

That last row is the point: the routing is enforced by the tool list, not by
hoping the prompt holds. No file ⇒ no invoice tool ⇒ the model cannot start a run
for a question that was really about a PO.

The attachment is described to the model as a *note* (filename + id), not sent as
an image block. Reading the document is the pipeline's own vision call with the
strict pinned extraction prompt — keeping that read out of the freeform chat turn
is what makes the extracted fields reproducible.

### What the model is allowed to say

`run_supplier_invoice_match` returns a deliberately **number-free** result:
outcome, variance types, match method, flags. Not one quantity or price. The full
struct goes to the UI through a side channel (`panels` on the response) instead.
So the model narrates *what happened* and points at the panel; it has no figures
to restate or hallucinate. Same split as the variance email, for the same reason.

### The result panel (side drawer)

An invoice run adds a **clickable card** to the chat reply. Clicking it opens a
drawer on the right that narrows the chat and collapses the left sidebar — the
conversation and the result stay side by side, the way opening a file works in
Claude. Under 900px the drawer takes the screen instead.

The drawer holds, top to bottom:

1. **Agent trace, live.** The `run_id` is minted *client-side* and sent with the
   turn, so the UI starts polling `/api/invoice/trace/{run_id}` the moment it
   sends and the steps stream in while the request is still in flight — not a
   finished log dumped at the end. Polling is forward-only from the last `seq`
   rendered, so no step repeats and none is missed.
2. **Extracted fields** — value + per-field confidence, coloured against the
   threshold.
3. **Server-computed figures** — ordered / received / invoiced, PO vs invoice unit
   price, line amount, from the matcher's `computed` struct.
4. **Variances** (taxonomy tag + detail), or **candidate PO lines** when the line
   could not be pinned down.
5. On a clean match, a **Continue to posting** button — the optional step 2. It
   is an offer, not a next step the panel takes for the user: clicking it is
   what runs the pre-flight checks, so someone who only wanted the match never
   sees a posting blocker. It posts nothing.
6. After that click, the **posting proposal** verbatim with an **Approve & post
   to SAP** button — that click is the human approval, and the only path that
   reaches `commit_post_supplier_invoice` — or the **blockers**, each with its
   remedy, if the pre-flight stopped it.

Nothing is computed client-side: every figure comes from the pipeline's structured
result or a trace event. The sidebar shows the live tolerances, so a runtime
override is visible without a restart or reload.

Cards stay clickable for the rest of the session, keyed on `run_id`, so a card
from three turns ago reopens *its* run. The trace itself lives in a capped
in-memory store — if it has been evicted, the panel says so and still shows the
result.

## Modules

| File | Pure? | Role |
|---|---|---|
| `credentials.py` | — | SAP credential source: Secrets Manager (default) / `.env` (LOCAL_DEV) |
| `invoice_extraction.py` | — | Nova-vision OCR → fields with confidences; confidence gate; fallback signal |
| `invoice_matching.py` | ✅ | 3-way match, fixed variance taxonomy, tolerances (reuses `compare_po_to_receipts`) |
| `invoice_preflight.py` | ✅ | **Tier 1** — deterministic pre-flight checks before any SAP call; decides the reference shape |
| `invoice_error_translation.py` | mostly | **Tier 2** — deterministic error map, then one bounded LLM translation of an unmapped SAP rejection |
| `invoice_posting.py` | ✅ | Build/hash the v2 deep-insert payload, render the approval summary, parse response |
| `invoice_email.py` | mostly | Server-rendered facts, prose prompt, **numeral guard**, per-run cap, SES send |
| `invoice_ledger.py` | — | Duplicate + commit-idempotency ledger; email dedup ledger |
| `invoice_matches.py` | — | Finished-match store: the seam between step 1 (match) and step 2 (post) |
| `invoice_proposals.py` | — | Single-use proposal store (the write boundary; mirrors `previews.py`) |
| `invoice_review.py` | — | Human review queue (JSON) |
| `invoice_read.py` | — | Live SAP duplicate cross-check (read-only; mirrors `gr.py`) |
| `invoice_trace.py` | — | In-memory, pollable agent trace |
| `invoice_tools.py` | — | Plain-Python tools + `run_invoice_pipeline` orchestrator |
| `attachments.py` | — | TTL'd in-memory store for files attached to a chat turn (ids travel, bytes don't) |
| `tools.py` | — | The LLM's tool list. Gates the invoice tool on an attachment being present |

All tool functions have clean typed signatures with **no** FastAPI/framework
objects inside the logic (`app/main.py` only translates HTTP ↔ these calls) —
this is what keeps an AgentCore-Gateway migration a wrap step, not a rewrite.
Keep it that way if you edit `invoice_tools.py`.

## Propose / commit gate

Identical rule to the PO layer. `propose_post_supplier_invoice` builds the full
payload + a human-readable summary and mints a single-use `proposal_id`; it
writes nothing. `commit_post_supplier_invoice` takes **only** a `proposal_id`,
is **never** placed in any LLM tool list, and fires from application code
(`POST /api/invoice/commit/{proposal_id}`) after explicit human approval —
**even when the match is clean**. A clean match lowers risk; it does not remove
the approval step. `run_invoice_pipeline` stops at the proposal and never
commits on its own.

This is what makes it safe to let the model start a run at all. `tools.py`
otherwise excludes every write path (PO preview/create are deliberately absent
because a freeform turn can't guarantee the confirmation pause). The invoice tool
is the one exception, and only because it *cannot write*: the model may decide to
**run a match**; only a human clicking Continue to posting can even start the
posting checks, and only a human clicking Approve can post. Neither
`propose_posting_for_match` nor `commit_post_supplier_invoice` is in any tool
list, and a test pins that.

## Error handling: two tiers between "propose" and "call SAP"

Two real SAP rejections drove this. Both are now caught *before* SAP is called.

### Tier 1 — deterministic pre-flight (`invoice_preflight.py`, pure)

Runs inside `propose_post_supplier_invoice`, before the payload is built —
which, since the two-step split above, means it runs **only once a human has
asked to post**, never on a plain match. If any check fails, `propose` returns
a **blocked result** and SAP is never contacted.

| Reason code | Blocks when |
|---|---|
| `PAYMENT_TERMS_MISSING` | PO header `PaymentTerms` is empty |
| `GR_BASED_IV_UNKNOWN` | The GR-based IV flag could not be read at all |
| `GR_REFERENCE_MISSING` | GR-based IV is on but no usable goods receipt exists |
| `INVOICE_NOT_EXPECTED` | PO line's invoice-receipt indicator is off |
| `ITEM_FINALLY_INVOICED` | PO line is already marked finally invoiced |
| `SERVICE_BASED_IV` | Line needs a service-entry-sheet reference (not built here) |
| `PO_DELETED` / `PO_ITEM_DELETED` | Deletion flag on the header / the line |
| `PO_RELEASE_INCOMPLETE` | PO has not finished its release strategy |

Every blocker carries `{code, message, remedy}`. The `remedy` is mandatory by
design — a blocker nobody can act on is a dead end, which is the failure mode
this tier exists to remove. Independent failures are all reported at once rather
than one-at-a-time.

**Bug 1 — the reference shape.** Each PO item's GR-based Invoice Verification
flag decides how an invoice line may reference its origin:

* flag **off** (PO-based) → reference `PurchaseOrder` + `PurchaseOrderItem` only.
* flag **on** (GR-based) → *additionally* reference the goods receipt
  (`ReferenceDocument` + `-FiscalYear` + `-Item`).

The old code always built the GR-document reference, so every PO-based item was
rejected with *"Only fill ReferenceDocument/-FiscalYear/-Item if GR-based IV is
active"* plus *"Item not selectable"*. `run_preflight` now returns a
`reference_mode` and `build_supplier_invoice_payload` branches on it. On a
PO-based line the three keys are **omitted entirely, not sent blank** — SAP's
check is on their presence. The goods receipt is still used for the 3-way
quantity match; it just isn't referenced on the document, and a warning says so
rather than leaving the difference implicit.

**Bug 2 — missing payment terms.** Required to post, inherited from the vendor
master at PO creation. When blank, this is an upstream master-data gap, so the
pre-flight **blocks and explains** instead of defaulting a value — the same
"surface, don't guess" rule the entity resolver applies to ambiguous code
matches, just at pre-flight time rather than resolution time. Defaulting would
be the worst outcome available: payment terms drive the due date and cash
discount, so an invented value posts cleanly and is financially wrong.

### Tier 2 — LLM fallback translation (`invoice_error_translation.py`)

Only for what tier 1 could not know. When SAP rejects a commit anyway,
`commit_post_supplier_invoice` passes the error text through
`translate_posting_error`, which returns `{explanation, source}`:

* `known` — matched the deterministic map (no LLM call, identical wording every
  time, zero tokens). The map covers the two bugs above too, so even a payload
  built by another route gets a clear sentence.
* `llm` — one Bedrock Nova call (the backend already wired for this pipeline),
  no retry, ~220 tokens, raw error truncated to 1200 chars.
* `untranslated` — no map hit and the model was unavailable or failed;
  the explanation is then SAP's own message, unchanged.

**It is translation only, and that is structural rather than prompted:**
`translate_posting_error` takes *strings* and returns *strings* — no payload, no
`proposal_id`, no client, no callable — so there is nothing for a translation to
act on. The module imports nothing from `invoice_tools` (the dependency runs the
other way), so there is no import path from the LLM fallback to
`commit_post_supplier_invoice`; a test asserts this on the module's AST. It never
raises: a UX layer must not be able to turn a clear failure into an opaque one.
The retry hint shown to the user is fixed application text, never model output,
and a translation never re-posts or consumes the proposal.

### Choices left to judgment (as the brief allowed)

- **Where the flags come from.** `parse_po_status` was extended rather than a
  second PO read added — the existing read already does `$expand=_PurchaseOrderItem`
  with no `$select`, so the fields were already on the wire, unmapped. Zero extra
  SAP calls.
- **How they reach `propose`.** `match_invoice` attaches the raw facts as
  `posting_prerequisites` (facts, never verdicts); `propose` judges them. So the
  pre-flight cannot disagree with the PO line the match resolved, and
  `run_preflight` stays a pure function a test can hand a plain dict.
- **Tri-state flags.** `po._flag()` returns `True` / `False` / `None`; an absent
  field is *not* read as `False`. An unknown GR-based-IV flag is a blocker, not a
  default — both guesses are rejections, so there is no safe fallback.
- **`build_supplier_invoice_payload` raises on an unset `reference_mode`** rather
  than defaulting. Defaulting would reintroduce the bug *silently*: the payload
  would still look well-formed and only SAP would find out.
- **Blocked ≠ failed.** A blocked posting is its own outcome (`"blocked"`),
  queued for a human with its blockers, and carries `sap_called: false` —
  distinct from `proposal_failed` (the payload couldn't be built) and from a
  variance. It never mints a `proposal_id`, so there is nothing approvable
  behind it.
- **Blocked ≠ unmatched, either.** It is reachable only from step 2, so it never
  contradicts a clean match: the invoice *did* match, and what is blocked is the
  posting. The outcome banner says so.

> **Tenant note (my402225, checked live 2026-07-29):** `4500000002` and
> `4500000261` carry **empty `PaymentTerms`** and
> `InvoiceIsGoodsReceiptBased = false` (`4500000261` item 10 also has
> `InvoiceIsExpected = false`), so both block at tier 1 until payment terms are
> maintained in the vendor master — correct behaviour, not a bug in the checks.
> **Do not generalize that to the tenant:** `4500000012` — the PO a supplier
> invoice was actually posted against — has `PaymentTerms = 'NT30'` and
> `InvoiceIsGoodsReceiptBased = true`, and passes tier 1 cleanly as
> `reference_mode = gr-based`. Master-data completeness varies per PO here;
> check the PO in front of you. Field names were read from this tenant's live
> `$metadata`, not assumed.

## Credentials — Secrets Manager secret shape & IAM

The shipped build reads SAP credentials from AWS Secrets Manager (nothing
sensitive in `.env`). Set `LOCAL_DEV=true` to fall back to the `.env` `SAP_*`
values for local iteration. The secret is one JSON string:

```json
{
  "base_url": "https://myXXXXXX-api.s4hana.cloud.sap",
  "username": "<communication user>",
  "password": "<password>"
}
```

- Secret id in `SAP_SECRET_NAME` (default `sap-helper/sap-communication-user`).
- IAM permission the runtime role needs: **`secretsmanager:GetSecretValue`** on
  that secret's ARN. (Bedrock/SES need their own permissions — `bedrock:InvokeModel`,
  `ses:SendEmail`.)
- Fetched once and cached in memory for the process lifetime; restart to pick up
  a rotated secret (`credentials.clear_cache()` is the test/ops hook).
- Plugs in at the `SAPClient` credential seam (an optional `credentials` arg);
  `_build_auth()` remains the separate auth-*method* seam for the OAuth swap.

## Tolerance defaults (and why)

Config defaults, overridable at runtime (`POST /api/invoice/tolerances`, no
restart):

| Floor | Default | Why |
|---|---|---|
| `INVOICE_PRICE_TOLERANCE_PCT` | 2% | Absorbs FX/rounding drift on unit price |
| `INVOICE_PRICE_TOLERANCE_ABS` | 1.00 | Ignores sub-unit differences on cheap lines |
| `INVOICE_QUANTITY_TOLERANCE_PCT` | 2% | Small packing/rounding differences |
| `INVOICE_QUANTITY_TOLERANCE_ABS` | 0.0 | Quantities are expected exact by default |

**A variance is flagged only when it breaches BOTH the percentage and the
absolute floor.** Requiring both suppresses the two false positives that matter:
a large *percentage* on a tiny *amount* (rounding on a cheap line) and a large
*amount* at a trivial *percentage*. The breach is strict (`>`), so a delta
exactly equal to a floor is within tolerance. When a base value is missing (no
PO price), the check falls back to the absolute floor alone rather than silently
passing. Quantity is matched against **goods received** (you pay for what you
got), not the ordered quantity.

## The OCR / extraction prompt

`invoice_extraction.py` sends the image/PDF to the vision provider
(`BedrockProvider.generate_with_vision`, Nova) with a system prompt that:

- pins output to **English** and to a **single strict JSON object** (no prose,
  no fences) — deliberately, so field names stay stable and this pipeline never
  inherits the RAG layer's cross-language drift bug;
- returns `{"value": ..., "confidence": 0..1}` per field, with calibration
  guidance (1.0 only when unambiguous; < 0.5 when guessing/unclear);
- extracts `po_number, po_item, quantity, unit_price, currency, vendor_name,
  vendor_invoice_number, material_description`, and explicitly distinguishes the
  buyer's PO number from the vendor's own invoice number.

Critical fields are `po_number, po_item, quantity, unit_price`, all judged against
`INVOICE_CONFIDENCE_THRESHOLD` (0.75) — but they split by whether a bad read has a
recovery route, because a single gate would make the description fallback
unreachable:

| Group | Fields | Below threshold → |
|---|---|---|
| **Blocking** | `po_number`, `quantity`, `unit_price` | straight to the review queue — nothing downstream can recover a misread PO number, quantity or price |
| **Recoverable** | `po_item` | try the **material-description fallback** first; review only if there is no confidently-read description to fall back on |

When the fallback runs, the low-confidence item number is **discarded** rather than
passed to the matcher: feeding a misread number into an exact item match could
silently hit the *wrong* PO line, which is worse than falling back. The resolved
line is flagged `material-description-fallback` in the match record, surfaced as a
trace event, and repeated as a warning on the posting proposal — never treated as a
direct hit. The description is held to the same confidence threshold (matching on
text we could not read confidently would be exactly the guess the brief forbids,
and one knob beats inventing a second, softer one).

`low_confidence` in the tool result still reports **every** critical field below
threshold (the honest picture for the trace and the review record);
`blocking_low_confidence` is the subset that actually stops the run.

## SES sandbox setup (verified addresses)

SES starts in **sandbox mode** and only delivers to **verified** addresses —
that constraint is the safety net for this POC, not a limitation to engineer
around. To enable the variance email:

1. In the SES console (region = `SES_REGION`), **verify the sender** address →
   set it as `SES_SENDER`.
2. **Verify every recipient** (sandbox requires this) → put them,
   comma-separated, in `INVOICE_EMAIL_RECIPIENTS`.
3. Leave the account in sandbox for the demo. (Production requests a limit
   increase to exit sandbox — do that only after human sign-off.)
4. Guards already in place: a per-run send cap (`INVOICE_EMAIL_MAX_SENDS_PER_RUN`,
   breach halts without partial sends), a dedup ledger (vendor + PO + item +
   variance types + rounded amounts), and a **numeral guard** — every figure is
   rendered server-side from the variance struct and any number the LLM's prose
   invents that isn't in those facts blocks the send.

With `SES_SENDER`/`INVOICE_EMAIL_RECIPIENTS` unset the pipeline still runs; it
just reports "No verified SES recipients configured" instead of emailing.

## Supplier Invoice API fields flagged during metadata review

From `API_SUPPLIERINVOICE_PROCESS_SRV/$metadata` on this tenant:

- **Only `A_SupplierInvoice` is creatable.** All item/tax/GL sets are
  `creatable=false` — they are deep-inserted through the header's navigation
  properties, not POSTed on their own.
- **PO-referenced lines** ride on the header POST via nav property
  **`to_SuplrInvcItemPurOrdRef`** (type `A_SuplrInvcItemPurOrdRefType`).
- **Vendor's own invoice number = `SupplierInvoiceIDByInvcgParty`**, and it is
  **MaxLength=16** — an OCR value longer than 16 chars 400s both the duplicate
  filter and the POST, so the live cross-check skips + flags it (`invoice_tools.py`).
- **Supplier = `InvoicingParty`**; tax is left to SAP via
  **`TaxIsCalculatedAutomatically=true`** + a per-line `TaxCode`.
- **OData v2 quirks** honoured in `invoice_posting.py`: `Edm.Decimal` as JSON
  strings, `Edm.DateTime` as `/Date(epoch-ms)/`, deep-insert children as a plain
  array, `{"d": {...}}` response envelope.
- **Lifecycle function imports exist** — `Post`, `Release`, `Cancel` (each takes
  `SupplierInvoice` + `FiscalYear`). The deep-insert create posts the document
  directly for the POC; these are the hooks for a park-then-post or cancel flow
  later.
- **Tax/gross balance caveat**: the POC sets `InvoiceGrossAmount` = net line
  total, which balances only under a **0%-rated** tax code (`INVOICE_DEFAULT_TAX_CODE`,
  default `V0`). A non-zero rate must be grossed-up or SAP's balance check rejects
  the document.
- **`to_SuplrInvcItemPurOrdRef` needs `ReferenceDocument`/`ReferenceDocumentFiscalYear`/
  `ReferenceDocumentItem` filled *when — and only when — the PO item is GR-based
  invoice verification*, in addition to `PurchaseOrder`/`PurchaseOrderItem` — and
  this triple is the Material Document (goods receipt), not the PO.**
  > ⚠ **Corrected 2026-07-29.** This finding was originally written as an
  > unconditional rule, and the code followed it unconditionally. That is bug 1:
  > PO `4500000012` happened to be a **GR-based** line, so the rule was only ever
  > verified on that half of the fork. On a **PO-based** line the same triple is
  > rejected — *"Only fill ReferenceDocument/-FiscalYear/-Item if GR-based IV is
  > active"* + *"Item not selectable"*. The condition is the PO item's
  > `InvoiceIsGoodsReceiptBased` flag; see "Error handling: two tiers" above.
  > Both live POs on this tenant are PO-based, so the unconditional version
  > could never have posted here.

  Found live against PO `4500000012` (two rounds): first, posting with only
  `PurchaseOrder`/`PurchaseOrderItem` set 400s with *"Fill in mandatory field
  'ReferenceDocument, -FiscalYear, -Item'"* from routine `MRM_FRSEG_CHECK`
  (Invoice Verification's preceding-document/FRSEG check). Filling
  `ReferenceDocument` with the **PO number** instead 400s again, now with
  *"Goods receipt item 4500000012 0000 0010 is invalid"* — this PO item is
  GR-based invoice verification, so the triple must be the actual
  `MaterialDocument`/`MaterialDocumentYear`/`MaterialDocumentItem` of the
  goods receipt backing the line (already fetched by `fetch_goods_receipt_items`
  in `gr.py` for the 3-way match, just never threaded through to posting).
  `invoice_matching.py`'s `_find_gr_reference` now picks the most recent
  non-reversed, non-cancelled receipt for the matched PO line and carries it
  in `computed`; `invoice_posting.py` uses it instead of the PO. If a line has
  more than one live receipt, the proposal carries a warning rather than
  silently trusting the pick — this pipeline posts one invoice item per PO
  line, not one per receipt, so a genuine multi-GR split isn't handled yet.
- **Company code & currency come from the PO**, not config defaults — live PO
  `4500000002` resolved to company `M100`, supplier `TST02`, currency **`IDR`**
  (this is an Indonesian tenant; the `USD`/`1710` demo defaults do not apply).
- **`TaxDeterminationDate`** (`Edm.DateTime`, header entity `A_SupplierInvoiceType`,
  `sap:label="Tax Date"`) is required on this tenant — it has time-dependent tax
  determination active, so a POST without it is rejected with *"Time-dependent
  taxes active: Fill Tax Determ. Date in invoice header."* `invoice_posting.py`
  now always sets it (default = posting date; editable in the approval UI like
  any other field).

### Editable proposal fields (approval UI)

`propose_post_supplier_invoice` returns a `fields` list (`build_proposal_fields`
in `invoice_posting.py`) alongside the plain-text `summary` — one row per
SAP field with its technical name, current value, whether a human may edit
it, and where the value came from:

- `confidence_kind: "ocr"` — read off the document by the vision extractor;
  `confidence_pct` is that field's real 0–100 OCR confidence (vendor invoice
  number, currency, gross/line amounts, quantity).
- `confidence_kind: "po"` / `"system"` — sourced from the matched PO or a
  config/posting default, not OCR; no percentage is shown (there is no
  confidence model for a deterministic value — showing one would imply false
  precision).
- `confidence_kind: "locked"` — `PurchaseOrder`/`PurchaseOrderItem`. Not
  editable: they are what the 3-way match resolved, and hand-editing either
  would silently detach the posting from the match that approved it.

`POST /api/invoice/proposal/{proposal_id}/edit` (`update_proposal_fields`)
applies a human's edits to a non-locked field, rebuilding the payload under
the **same** `proposal_id` — `commit_post_supplier_invoice` still takes only
that id, so the propose/commit approval boundary is unchanged by editing.

### Recovering from a misread PO number (previously a dead end)

An OCR-read PO number that doesn't exist on the tenant (`_fetch_po_status`
gets a 404) is a very common failure mode — long digit strings with runs of
zeros are a known weak spot for vision extraction, and the model can be
**confidently wrong** (a 0.95-confidence read can still drop a digit), so the
confidence gate doesn't catch it. Previously this dead-ended the whole run
with a bare error the user could only read and re-upload from scratch.

It now behaves like the existing `po_line_not_found`/`ambiguous_po_line`
cases: `run_invoice_pipeline` parks it in the review queue as `po_not_found`
with everything else already read from the invoice (`values`, `extracted`),
and `POST /api/invoice/review/{review_id}/retry` (`retry_match_with_corrected_po`)
lets a human retype just the PO number and re-run the match — without
re-uploading the document. All three PO-match review reasons share this retry
path, since they all store the same recoverable state.

A successful retry mints its own trace `run_id` and continues through
`_resume_after_match` — the exact same duplicate-check → match/variance tail
`run_invoice_pipeline` uses, refactored out so the two entry points can't drift
apart. It lands on the same "matched, post separately" stop; a corrected PO
number does not skip the posting step or human approval.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/attachments` | Stage a file for the next chat turn → `attachment_id` (bytes stay server-side) |
| POST | `/api/mcp/query` | The chat turn. `attachments` + `run_id` ride along; an invoice run comes back in `panels` |
| GET | `/api/invoice/trace/{run_id}?after=N` | Poll the live agent trace (the UI calls this while the turn is in flight) |
| POST | `/api/invoice/propose/{match_id}` | **Step 2.** Run the posting pre-flight on a finished match and build the proposal. Writes nothing |
| POST | `/api/invoice/proposal/{proposal_id}/edit` | Apply human edits to a not-yet-approved proposal (locked fields rejected) |
| POST | `/api/invoice/commit/{proposal_id}` | **Human-approved** post to SAP (the only writer) |
| POST | `/api/invoice/review/{review_id}/retry` | Retry a parked PO-match review item with a human-corrected PO number |
| GET/POST | `/api/invoice/tolerances` | Read / override matching tolerances at runtime |
| GET | `/api/invoice/review` | Open human-review items |
| POST | `/api/invoice/process` | **Step 1**, direct entry (file → match result + `match_id`). Not used by the UI any more — kept as the programmatic/testing entry point |

## Bonus: pick the two demo invoices

```powershell
.\.venv\Scripts\python -m scripts.find_demo_invoices --max 40
```

Reuses the read-only PO/GR analysis (`get_po_gr_anomalies` + aging) to print one
clean fully-received PO line and one with a real variance — the two PO numbers to
build the demo's matching and mismatching invoice inputs from. (On this tenant it
surfaces clean PO `4500000002` and variance PO `4500000010`.)

## Implementation choices left to judgment

- **Confidence threshold 0.75** — high enough to catch a genuinely unclear read,
  low enough that a clean printed invoice sails through (validated: a clear
  synthetic invoice extracted at 0.95+).
- **Review queue / ledgers / trace** — JSON files and an in-memory pollable
  store, the same "simplest thing that shows the mechanism, becomes a table/queue
  in production" choice as the existing ledgers. The trace is deliberately not
  over-built (a capped in-memory store with a monotonic `seq` the UI polls).
- **What the review queue holds** — both refusals to guess, not just the OCR one:
  a low-confidence blocking field, *and* a PO line that came back not-found or
  ambiguous. Returning candidates in the HTTP response alone would lose the case
  the moment the response is gone; queueing it is what makes "don't guess" an
  actual handoff to a person rather than a dead end. Items are append-only and
  read via `GET /api/invoice/review` — resolving one is a human action in SAP,
  so there is no "close" write path in the POC.
- **Per-run mailer** — a fresh `SESMailer` per pipeline run, so the send cap is
  genuinely per-run rather than per-process.
- **Duplicate identity — two keys, one reference.** Each ledger entry is keyed on
  *both* normalised vendor text + reference *and* supplier code + reference,
  because the two identities arrive at different times and fail differently: the
  vendor text is known immediately but drifts with OCR ("Acme Ltd" vs "Acme
  Limited"), the supplier code is stable but unknown until the PO lookup. So the
  pipeline checks three times, cheapest first — text key before any SAP read,
  party key right after the lookup, then the authoritative live SAP cross-check.
  Recording under one identity and looking up under the other is a silent
  failure (the check simply never fires), so `record()` writes both keys and the
  `vendor` it is given must be the text read off the document.

## Known limits (Supplier Invoice)

- Single invoice line per document in the POC (the matcher and payload are
  per-line; multi-line is an additive extension).
- Price comparison is per unit price vs PO `NetPriceAmount`; price-unit
  quantities > 1 and delivery-cost conditions are not modelled.
- Vendor mismatch is only assertable when the PO exposes a supplier name; when it
  doesn't (Business Partner name API not authorised here) the vendor is reported
  as unverified rather than a fabricated mismatch.
- Same post-commit timeout gap as the PO create path (see PO "Known POC limits").

## Prior next-step, still open: writing Goods Receipts

`API_MATERIAL_DOCUMENT_SRV` (SAP_COM_0108) read access exists (`gr.py`).
Creating a GR (`preview_goods_receipt`, movement type `101`, same
preview→confirm→create boundary and `SAPClient.post` CSRF path, a new ledger
namespace) is still not built — out of scope for this flow, which only *reads*
existing goods receipts.

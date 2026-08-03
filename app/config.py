"""Central configuration.

Every deployment-specific value lives here and is overridable via
environment variables or a local `.env` file (see `.env.example`).
Nothing elsewhere in the codebase reads os.environ directly.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- Generation layer -------------------------------------------------
    # Which backend answers questions: "bedrock" or "groq".
    generation_provider: str = "bedrock"

    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    aws_region: str = "us-east-1"
    # Optional explicit AWS keys. Leave empty to use boto3's default chain
    # (real env vars, ~/.aws/credentials, SSO, instance role). Values in .env
    # are NOT process env vars, so they must be passed to boto3 explicitly.
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    # Bedrock model IDs carry the provider prefix. Amazon Nova requires a
    # cross-region inference profile id (the "us." prefix), not the bare id.
    # Alternatives: us.amazon.nova-lite-v1:0 (cheaper), us.amazon.nova-micro-v1:0.
    bedrock_model_id: str = "us.amazon.nova-pro-v1:0"

    generation_max_tokens: int = 1500

    # --- Conversation memory (client-held history, see static/index.html) --
    # Hard server-side cap regardless of what the client sends.
    history_max_turns: int = 10
    # Total chars of prior-turn text injected into the answer prompt.
    history_char_budget: int = 2000
    # Small: the condensation call only outputs a rewritten search query.
    condense_max_tokens: int = 120

    # --- Retrieval core ----------------------------------------------------
    # Self-hosted, open-source embedding model (ONNX via fastembed).
    embedding_model: str = "BAAI/bge-small-en-v1.5"

    # Empty QDRANT_URL -> embedded Qdrant persisted at qdrant_path (zero-infra demo).
    # Set QDRANT_URL (and optionally QDRANT_API_KEY) to point at a Qdrant
    # server/cluster - the code path is identical.
    qdrant_url: str = ""
    qdrant_api_key: str = ""
    qdrant_path: str = "./data/qdrant"
    qdrant_collection: str = "sap_notes"

    top_k: int = 5
    # Structure-aware chunking bounds (characters).
    max_chunk_chars: int = 1800
    chunk_overlap_chars: int = 200

    # --- Knowledge base selection --------------------------------------------
    # Which retrieval backend answers a query: "local" (the Qdrant + fastembed
    # store above) or "bedrock" (Amazon Bedrock Knowledge Base). The chat UI
    # toggle overrides this per-request; this is just the startup default.
    knowledge_base: str = "local"
    # Bedrock Knowledge Base id (from the AWS console). Required for the
    # "bedrock" option; reuses aws_region / aws_access_key_id / aws_secret_access_key above.
    bedrock_kb_id: str = ""

    # --- Ingestion ----------------------------------------------------------
    notes_dir: str = "./data/notes"
    ledger_path: str = "./data/ingestion_ledger.json"

    # --- Citations ------------------------------------------------------------
    # SAP Note URL pattern; {note_id} is substituted. Config, not hardcoded,
    # because the client portal may expose notes under a different launcher URL.
    sap_note_url_template: str = "https://me.sap.com/notes/{note_id}"

    # --- SAP transactions (MCP server, app/mcp/) ---------------------------
    # Tenant API endpoint, e.g. https://myXXXXXX-api.s4hana.cloud.sap
    # (the -api host, not the UI host). Empty -> transaction tools report
    # themselves unconfigured instead of failing at import time.
    sap_base_url: str = ""
    # Communication-user credentials (Basic Auth). POC only - see app/mcp/README.md.
    sap_username: str = ""
    sap_password: str = ""
    # Auth mechanism: "basic" today. The OAuth fields below are reserved so a
    # future "oauth" branch in SAPClient._build_auth() is config-only.
    sap_auth_method: str = "basic"
    sap_oauth_client_id: str = ""
    sap_oauth_client_secret: str = ""
    sap_oauth_token_url: str = ""
    sap_http_timeout_seconds: float = 30.0

    # OData service paths - verify against your tenant's released APIs
    # (Communication Arrangements) before relying on these defaults.
    # PO create/read is OData v4 (API_PURCHASE_ORDER_2); the item navigation
    # property is "_PurchaseOrderItem" (server.py, po.py) - if your tenant only
    # released the older v2 API_PURCHASEORDER_PROCESS_SRV instead, that service
    # uses "A_PurchaseOrder" / "to_PurchaseOrderItem" and po.py/server.py need
    # the v2 names swapped back in.
    sap_po_service: str = "/sap/opu/odata4/sap/api_purchaseorder_2/srvd_a2x/sap/purchaseorder/0001"
    sap_supplier_service: str = "/sap/opu/odata/sap/API_BUSINESS_PARTNER"
    sap_product_service: str = "/sap/opu/odata/sap/API_PRODUCT_SRV"
    # Plant master data is only released as OData v4 in S/4HANA Cloud; the
    # resolver handles the v2/v4 dialect difference per lookup spec.
    sap_plant_service: str = "/sap/opu/odata4/sap/api_plant/srvd_a2x/sap/plant/0001"
    # Goods Receipt / Material Document (OData v2) - used for the PO<->GR
    # two-way match (compare_po_to_goods_receipts); read-only, no writes yet.
    sap_gr_service: str = "/sap/opu/odata/sap/API_MATERIAL_DOCUMENT_SRV"
    # Language key for product-description search (A_ProductDescription).
    sap_material_search_language: str = "EN"
    # Candidates returned per master-data lookup before the resolver reports
    # ambiguity to the user.
    sap_resolver_max_candidates: int = 5

    # Read-only PO/GR analysis (app/mcp/analysis.py). Each analysed PO costs one
    # Goods-Receipt read on top of the single PO-collection read, so this caps
    # how many POs a report tool will pull before summarising.
    sap_analysis_max_pos: int = 50
    # Default short-delivery sensitivity: a receipt short by >= 10% of the
    # ordered quantity is flagged. Over-deliveries are always flagged.
    sap_analysis_variance_threshold: float = 0.10

    # Document defaults applied when the user doesn't specify them. These are
    # the SAP Best Practices US model-company values (company 1710); they WILL
    # differ on a real tenant - verify against customizing (see app/mcp/README.md).
    sap_default_company_code: str = "1710"
    sap_default_purchasing_org: str = "1710"
    sap_default_purchasing_group: str = "001"
    sap_default_order_type: str = "NB"
    sap_default_currency: str = "USD"

    # Two-step write safety: previews expire after this many seconds.
    sap_preview_ttl_seconds: int = 1800
    # Idempotency ledger (same JSON-ledger pattern as ingestion).
    sap_po_ledger_path: str = "./data/po_idempotency_ledger.json"

    # How the MCP server is exposed when run via `python -m app.mcp.server`:
    # "stdio" (default; for Claude Desktop/CLI-style clients) or
    # "streamable-http" (serves /mcp on sap_mcp_host:sap_mcp_port).
    sap_mcp_transport: str = "stdio"
    sap_mcp_host: str = "127.0.0.1"
    sap_mcp_port: int = 8001

    # --- Credential source (app/mcp/credentials.py) -----------------------
    # Manager requirement: the shipped build reads SAP (and AWS) credentials
    # from AWS Secrets Manager, NOT from .env. LOCAL_DEV=true flips back to the
    # .env values above so local iteration needs no AWS access. See README.
    local_dev: bool = False
    # Secrets Manager secret id/ARN holding a JSON object with keys
    # base_url / username / password (see README "Secrets Manager secret shape").
    sap_secret_name: str = "sap-helper/sap-communication-user"

    # Chat attachments (the composer's + button, app/mcp/attachments.py). An
    # invoice photo from a phone is comfortably under this; the cap exists so a
    # stray large upload can't sit in the in-memory store.
    attachment_max_bytes: int = 12 * 1024 * 1024

    # --- Supplier Invoice matching agent (app/mcp/invoice_*.py) -----------
    # Accounts-Payable Supplier Invoice service (OData v2, SAP_COM_0057). This
    # is the correct 3rd leg for a PO->GR->Invoice 3-way match - NOT the Sales
    # BillingDocument API. Verified against this tenant's $metadata: header set
    # A_SupplierInvoice, PO-reference items via nav prop to_SuplrInvcItemPurOrdRef.
    sap_supplier_invoice_service: str = "/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV"

    # Matching tolerances (env defaults; overridable at runtime via
    # POST /api/invoice/tolerances - no restart). A variance is flagged only if
    # it breaches BOTH the percentage and absolute floors, so trivial rounding
    # (a few cents) never trips a false positive. Rationale in the README.
    invoice_price_tolerance_pct: float = 0.02       # 2% unit-price drift allowed
    invoice_price_tolerance_abs: float = 1.00       # ...and at least this much (doc currency)
    invoice_quantity_tolerance_pct: float = 0.02    # 2% qty drift vs goods received
    invoice_quantity_tolerance_abs: float = 0.0     # exact by default (whole units)

    # Extraction: any of PO number / item / quantity / price below this
    # confidence routes the invoice to the human review queue instead of
    # proceeding on a best guess.
    invoice_confidence_threshold: float = 0.75
    # Review-queue store (JSON, same durable-ledger pattern as the PO ledger).
    invoice_review_queue_path: str = "./data/invoice_review_queue.json"

    # Duplicate / idempotency ledgers (extend the PO-ledger pattern).
    sap_supplier_invoice_ledger_path: str = "./data/supplier_invoice_ledger.json"
    sap_invoice_email_ledger_path: str = "./data/invoice_email_ledger.json"

    # Posting defaults. The POC posts with automatic tax calculation and a
    # single tax code per line; a 0%-rate input-tax code keeps gross == net so
    # the MIRO balance check passes on the demo tenant. Verify against your tax
    # customizing (see README) - a non-zero rate needs the gross adjusted.
    invoice_default_tax_code: str = "V0"
    invoice_accounting_document_type: str = "RE"    # supplier invoice (Rechnung)

    # --- Variance email (Amazon SES, app/mcp/invoice_email.py) ------------
    # SES starts in SANDBOX mode: it will only deliver to verified addresses.
    # That sandbox IS the safety net for this POC - keep it until a human has
    # signed off on going live. See README "SES sandbox setup".
    ses_region: str = "us-east-1"
    ses_sender: str = ""                            # a verified "From" address
    # Comma-separated verified recipients the variance mail may go to.
    invoice_email_recipients: str = ""
    # Hard cap on emails a single pipeline run may send; a breach halts sending
    # and logs rather than partially blasting.
    invoice_email_max_sends_per_run: int = 5
    # Pin the variance-email language explicitly (there is an unrelated known
    # bug where RAG answers drift across languages - this pipeline must not
    # inherit it). Any human-readable language name the model understands.
    invoice_email_language: str = "English"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()

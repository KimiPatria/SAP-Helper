"""The MCP server: four tools implementing lookup -> preview -> create -> status.

Run standalone:  python -m app.mcp.server
Transport comes from SAP_MCP_TRANSPORT ("stdio" default; "streamable-http"
serves /mcp on SAP_MCP_HOST:SAP_MCP_PORT for an HTTP-capable chat layer).

Design decisions (rationale in README.md):
* preview and create are separate tools, and create accepts ONLY a
  preview_id - never field values - so "proposed" vs "executed" is a hard
  tool boundary, not a flag.
* Tools return structured dicts with "ok" plus actionable error text
  instead of raising, so the calling LLM can recover (re-ask the user,
  re-preview) rather than see an opaque protocol error.
* Dependencies are built lazily on first tool call: importing this module
  (or listing tools) works without SAP credentials; only actual use
  reports the missing configuration.

IMPORTANT for stdio transport: stdout carries the MCP protocol - all
logging goes to stderr (Python's default). Never print() in tool code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

from pydantic import BaseModel, Field

from mcp.server.fastmcp import FastMCP

from app.config import settings
from app.mcp.analysis import (
    build_po_aging_report,
    build_supplier_performance_report,
    detect_po_gr_anomalies,
)
from app.mcp.client import SAPClient
from app.mcp.credentials import CredentialError, resolve_sap_credentials
from app.mcp.errors import SAPRequestError
from app.mcp.gr import fetch_goods_receipt_items
from app.mcp.po import (
    ResolvedItem,
    ResolvedPO,
    build_odata_payload,
    compare_po_to_receipts,
    normalize_quantity,
    parse_create_response,
    parse_po_status,
    payload_hash,
    render_preview,
)
from app.mcp.previews import PreviewStore
from app.mcp.registry import IdempotencyRegistry
from app.mcp.resolver import Candidate, MasterDataResolver, build_specs

log = logging.getLogger("sap-mcp")

mcp = FastMCP(
    "sap-transactions",
    instructions=(
        "Tools for creating Purchase Orders in SAP S/4HANA Cloud from natural "
        "language. Required workflow: (1) optionally use search_master_data to "
        "explore codes; (2) call preview_purchase_order with the user's "
        "request - it resolves free-text names to SAP codes and returns a "
        "human-readable summary plus a preview_id, or asks for clarification "
        "if a name is ambiguous; (3) show the summary to the user VERBATIM and "
        "wait for their explicit confirmation; (4) only then call "
        "create_purchase_order with the preview_id. Never call "
        "create_purchase_order without the user having seen and confirmed the "
        "exact preview. Use get_purchase_order_status to check documents "
        "afterwards."
    ),
)


@dataclass
class _Deps:
    client: SAPClient
    resolver: MasterDataResolver
    previews: PreviewStore
    registry: IdempotencyRegistry


@lru_cache(maxsize=1)
def _deps() -> _Deps:
    # Credentials come from the credential-source seam: Secrets Manager by
    # default, .env only under LOCAL_DEV. A CredentialError is re-raised as a
    # SAPRequestError so every tool's existing `except SAPRequestError` path
    # surfaces one plain sentence (they never special-case credential errors).
    try:
        credentials = resolve_sap_credentials(settings)
    except CredentialError as exc:
        raise SAPRequestError(str(exc)) from exc
    client = SAPClient(
        base_url="",
        username="",
        password="",
        auth_method=settings.sap_auth_method,
        timeout_seconds=settings.sap_http_timeout_seconds,
        credentials=credentials,
    )
    return _Deps(
        client=client,
        resolver=MasterDataResolver(
            client, build_specs(settings), settings.sap_resolver_max_candidates
        ),
        previews=PreviewStore(settings.sap_preview_ttl_seconds),
        registry=IdempotencyRegistry(settings.sap_po_ledger_path),
    )


def _candidate_dict(c: Candidate) -> dict:
    return {"code": c.code, "description": c.description, "match": c.match}


def _pick(candidates: list[Candidate]) -> Candidate | None:
    """A candidate is auto-usable only when unambiguous: an exact code/name
    hit, or the single match. Anything else goes back to the user."""
    if not candidates:
        return None
    if candidates[0].match in ("exact-code", "exact-name") or len(candidates) == 1:
        return candidates[0]
    return None


class POItemInput(BaseModel):
    """One purchase order line as extracted from the user's request."""

    material: str = Field(description=(
        "Material as the user referred to it - free text ('hex bolts M8') or "
        "an exact SAP product code ('TG11'). Resolved against SAP master data."
    ))
    quantity: float | str = Field(description="Order quantity, e.g. 10 or '2.5'.")
    unit: str = Field(default="", description=(
        "Unit of measure (EA, PC, KG...) only if the user stated one; leave "
        "empty to let SAP use the material's default unit."
    ))
    net_price: float | str | None = Field(default=None, description=(
        "Net price per unit only if the user stated one; leave empty to let "
        "SAP price from the purchasing info record."
    ))


@mcp.tool()
def search_master_data(entity_type: str, description: str) -> dict:
    """Look up SAP master data codes from a free-text description. Read-only.

    Use when the user mentions a supplier, material, or plant by name and you
    want to explore what exists, or to disambiguate before a preview. (You do
    NOT need to call this before preview_purchase_order - preview resolves
    names itself - but it is useful when the user asks 'which suppliers do we
    have for X?' or a previous preview reported ambiguity.)

    Args:
        entity_type: One of "supplier", "material", "plant".
        description: Free text ("domestic steel") or an exact code ("17300001").

    Returns:
        {ok, entity_type, query, candidates: [{code, description, match}], guidance}
        where match is exact-code | exact-name | name-match | partial, best
        first. Empty candidates means nothing plausible exists - ask the user
        for a different name or the SAP code rather than guessing.
    """
    try:
        deps = _deps()
        candidates = deps.resolver.resolve(entity_type.strip().lower(), description)
    except SAPRequestError as exc:
        return {"ok": False, "error": exc.user_message}
    if not candidates:
        guidance = (
            f"No {entity_type} found matching '{description}'. Ask the user "
            "for a different name, a distinctive word from the name, or the SAP code."
        )
    elif len(candidates) == 1 or candidates[0].match in ("exact-code", "exact-name"):
        guidance = "Unambiguous - safe to use the first candidate."
    else:
        guidance = (
            "Multiple plausible matches - show them to the user and let them "
            "choose; do not silently pick one."
        )
    return {
        "ok": True,
        "entity_type": entity_type,
        "query": description,
        "candidates": [_candidate_dict(c) for c in candidates],
        "guidance": guidance,
    }


@mcp.tool()
def preview_purchase_order(
    supplier: str,
    items: list[POItemInput],
    plant: str,
    currency: str = "",
    order_type: str = "",
    company_code: str = "",
    purchasing_org: str = "",
    purchasing_group: str = "",
) -> dict:
    """Resolve a natural-language PO request into a confirmable proposal.
    Writes NOTHING to SAP.

    Use whenever the user asks to order/buy/purchase something. Supplier,
    materials and plant may be free text or exact codes - this tool resolves
    them against SAP master data. Leave the org fields empty unless the user
    specified them; configured tenant defaults apply.

    Args:
        supplier: Supplier as the user said it ("the steel supplier") or code.
        items: The order lines (see POItemInput).
        plant: Receiving plant, free text ("main warehouse") or code ("1710").
        currency / order_type / company_code / purchasing_org / purchasing_group:
            Only if the user explicitly specified them.

    Returns:
        On success: {ok: true, preview_id, summary, resolved, warnings,
        instructions}. Show `summary` to the user VERBATIM, then - only after
        their explicit confirmation - call create_purchase_order(preview_id).
        On ambiguity: {ok: false, needs_clarification: [{field, query,
        candidates}]} - present the candidates and re-call this tool with the
        user's choice (use the code). Never pick between candidates yourself.
    """
    try:
        deps = _deps()
    except SAPRequestError as exc:
        return {"ok": False, "error": exc.user_message}
    if not items:
        return {"ok": False, "error": "items must contain at least one line - "
                "re-call with the material(s) and quantity the user asked for."}

    needs_clarification: list[dict] = []
    warnings: list[str] = []

    def resolve_one(entity_type: str, text: str, field_name: str) -> Candidate | None:
        candidates = deps.resolver.resolve(entity_type, text)
        chosen = _pick(candidates)
        if chosen is None:
            needs_clarification.append({
                "field": field_name,
                "query": text,
                "candidates": [_candidate_dict(c) for c in candidates],
            })
        return chosen

    try:
        supplier_c = resolve_one("supplier", supplier, "supplier")
        plant_c = resolve_one("plant", plant, "plant")
        resolved_items: list[ResolvedItem] = []
        for index, item in enumerate(items):
            material_c = resolve_one("material", item.material, f"items[{index}].material")
            try:
                quantity = normalize_quantity(item.quantity, f"items[{index}].quantity")
                price = normalize_quantity(item.net_price, f"items[{index}].net_price") \
                    if item.net_price not in (None, "") else ""
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if material_c:
                resolved_items.append(ResolvedItem(
                    material_code=material_c.code,
                    material_description=material_c.description,
                    quantity=quantity,
                    unit=item.unit.strip().upper(),
                    net_price=price,
                ))
    except SAPRequestError as exc:
        return {"ok": False, "error": exc.user_message}

    if needs_clarification:
        return {
            "ok": False,
            "needs_clarification": needs_clarification,
            "message": (
                "Some references could not be resolved to exactly one SAP "
                "record. For each field: if candidates are listed, ask the "
                "user to choose; if empty, ask for a different name or code. "
                "Then re-call preview_purchase_order using the chosen codes."
            ),
        }

    resolved = ResolvedPO(
        supplier_code=supplier_c.code,
        supplier_name=supplier_c.description,
        plant_code=plant_c.code,
        plant_name=plant_c.description,
        company_code=company_code.strip() or settings.sap_default_company_code,
        purchasing_org=purchasing_org.strip() or settings.sap_default_purchasing_org,
        purchasing_group=purchasing_group.strip() or settings.sap_default_purchasing_group,
        order_type=order_type.strip().upper() or settings.sap_default_order_type,
        currency=currency.strip().upper() or settings.sap_default_currency,
        items=tuple(resolved_items),
    )
    if not (company_code and purchasing_org and purchasing_group):
        warnings.append(
            f"Using configured tenant defaults: company code "
            f"{resolved.company_code}, purchasing org {resolved.purchasing_org}, "
            f"group {resolved.purchasing_group}."
        )
    if any(not i.net_price for i in resolved.items):
        warnings.append(
            "No price given for some items - SAP will price them from the "
            "purchasing info record; creation fails if none exists for that "
            "material/supplier combination."
        )

    payload = build_odata_payload(resolved)
    digest = payload_hash(payload)
    twin = deps.registry.find_by_payload(digest)
    if twin:
        warnings.append(
            f"An identical PO ({twin['purchase_order']}) was already created "
            f"via this assistant at {twin['created_at']}. Confirm the user "
            "really wants a second, separate order."
        )

    summary = render_preview(resolved, warnings)
    entry = deps.previews.put(payload, digest, summary)
    log.info("Preview %s minted (payload %s...)", entry.preview_id, digest[:12])
    return {
        "ok": True,
        "preview_id": entry.preview_id,
        "summary": summary,
        "resolved": payload,
        "warnings": warnings,
        "instructions": (
            "Show `summary` to the user verbatim and ask for confirmation. "
            "Only after an explicit yes, call "
            f"create_purchase_order(preview_id='{entry.preview_id}'). If the "
            "user changes anything, call preview_purchase_order again instead."
        ),
    }


@mcp.tool()
def create_purchase_order(preview_id: str) -> dict:
    """Execute a previewed, user-confirmed purchase order in SAP. THE ONLY
    tool that writes.

    Use strictly after preview_purchase_order returned this preview_id AND the
    user explicitly confirmed that exact preview. This tool takes no field
    values by design: anything not previewed cannot be created. Safe to retry
    with the same preview_id after a transient failure - an idempotency ledger
    guarantees at most one PO per confirmed preview.

    Args:
        preview_id: The id returned by preview_purchase_order.

    Returns:
        {ok: true, purchase_order, message} on success (purchase_order is the
        SAP document number - report it to the user). If the preview was
        already executed, returns the existing document with
        duplicate_suppressed: true instead of creating another. On failure:
        {ok: false, error, retry_hint}.
    """
    try:
        deps = _deps()
    except SAPRequestError as exc:
        return {"ok": False, "error": exc.user_message}

    entry, reason = deps.previews.claim(preview_id)
    if entry is None:
        # The preview may be unclaimable precisely BECAUSE it was executed
        # (consumed, or lost in a restart) - answer a blind retry with the
        # document that exists rather than an error.
        executed = deps.registry.find_by_request(preview_id)
        if executed:
            return {
                "ok": True,
                "purchase_order": executed["purchase_order"],
                "duplicate_suppressed": True,
                "message": (
                    f"This preview was already executed at "
                    f"{executed['created_at']} - purchase order "
                    f"{executed['purchase_order']} exists in SAP. No second "
                    "document was created."
                ),
            }
        return {"ok": False, "error": reason}

    key = deps.registry.key(entry.payload_hash, preview_id)
    existing = deps.registry.find(key)
    if existing:
        deps.previews.mark_consumed(preview_id)
        return {
            "ok": True,
            "purchase_order": existing["purchase_order"],
            "duplicate_suppressed": True,
            "message": (
                f"This preview was already executed at {existing['created_at']} "
                f"- purchase order {existing['purchase_order']} exists in SAP. "
                "No second document was created."
            ),
        }

    try:
        data = deps.client.post(
            settings.sap_po_service,
            f"{settings.sap_po_service}/PurchaseOrder",
            entry.payload,
            context="creating the purchase order",
        )
    except SAPRequestError as exc:
        return {
            "ok": False,
            "error": exc.user_message,
            "retry_hint": (
                "The preview is still valid. If this looks transient (timeout, "
                "service unavailable), retry create_purchase_order with the "
                "same preview_id. If SAP rejected a field value, fix the "
                "request and start over with preview_purchase_order."
            ),
        }

    result = parse_create_response(data)
    po_number = result["purchase_order"]
    if not po_number:
        log.warning("Create returned 2xx but no PurchaseOrder number: %s", str(data)[:500])
        return {
            "ok": False,
            "error": (
                "SAP accepted the request but returned no document number. "
                "Check recent POs with get_purchase_order_status before retrying."
            ),
        }
    deps.registry.record(key, entry.payload_hash, preview_id, po_number)
    deps.previews.mark_consumed(preview_id)
    log.info("Created purchase order %s (preview %s)", po_number, preview_id)
    return {
        "ok": True,
        "purchase_order": po_number,
        "message": f"Purchase order {po_number} created in SAP. Report this "
                   "number to the user.",
    }


def _fetch_po_status(deps: _Deps, number: str) -> dict:
    """Shared by get_purchase_order_status and compare_po_to_goods_receipts -
    {ok: True, ...parse_po_status()} or {ok: False, error}."""
    try:
        data = deps.client.get(
            f"{settings.sap_po_service}/PurchaseOrder('{number}')",
            params={"$expand": "_PurchaseOrderItem"},
            context=f"reading purchase order {number}",
        )
    except SAPRequestError as exc:
        if exc.status_code == 404:
            # Flagged distinctly from other SAP errors: a 404 here is very
            # often a misread PO number (OCR dropped/transposed a digit),
            # which a human can correct and retry - see invoice_tools.py's
            # retry_match_with_corrected_po. A timeout/auth failure below is
            # not something re-typing a number fixes, so it stays unflagged.
            return {"ok": False, "not_found": True,
                    "error": f"Purchase order {number} does not "
                    "exist on this tenant. Check the number with the user."}
        return {"ok": False, "error": exc.user_message}
    return {"ok": True, **parse_po_status(data)}


@mcp.tool()
def get_purchase_order_status(purchase_order: str) -> dict:
    """Check an existing purchase order by document number. Read-only.

    Use when the user asks about a PO's status/contents, or to verify a
    document after creation.

    Args:
        purchase_order: The SAP PO number, e.g. "4500000123" (digits as
            returned by create_purchase_order).

    Returns:
        {ok: true, status, status_code, purchase_order, supplier, currency,
        net_amount, created_on, items: [...]}. `status` is a plain-language
        reading of SAP's processing status; NOT_FOUND errors mean the number
        does not exist on this tenant.
    """
    number = purchase_order.strip().replace("'", "")
    if not number:
        return {"ok": False, "error": "purchase_order must be a document number, "
                "e.g. '4500000123'."}
    try:
        deps = _deps()
    except SAPRequestError as exc:
        return {"ok": False, "error": exc.user_message}
    return _fetch_po_status(deps, number)


@mcp.tool()
def compare_po_to_goods_receipts(purchase_order: str) -> dict:
    """Two-way match: compare a purchase order's ordered quantities against
    what was actually received (Goods Receipt / Material Documents). Read-only.

    This is NOT a full 3-way match (Supplier Invoice access isn't configured
    on this tenant) - it only checks PO vs. Goods Receipt.

    Use when the user asks whether a PO was fully received, or to spot
    under/over-deliveries.

    Args:
        purchase_order: The SAP PO number, e.g. "4500000123".

    Returns:
        {ok: true, purchase_order, supplier, supplier_name, fully_matched,
        items: [{item, material, ordered, received, unit, status, matched}]}.
        `status` per item is "match", "short by <qty> <unit>",
        "over-received by <qty> <unit>", or "not yet received".
    """
    number = purchase_order.strip().replace("'", "")
    if not number:
        return {"ok": False, "error": "purchase_order must be a document number, "
                "e.g. '4500000123'."}
    try:
        deps = _deps()
    except SAPRequestError as exc:
        return {"ok": False, "error": exc.user_message}
    po_status = _fetch_po_status(deps, number)
    if not po_status.get("ok"):
        return po_status
    try:
        gr_rows = fetch_goods_receipt_items(
            deps.client, settings.sap_gr_service, number
        )
    except SAPRequestError as exc:
        return {"ok": False, "error": exc.user_message}
    return {"ok": True, **compare_po_to_receipts(po_status, gr_rows)}


# ---- read-only PO/GR analysis (aging, supplier perf, anomalies) ----------
# All three tools share one fetch: pull a bounded set of POs, then net each
# one's goods receipts via the existing 2-way match. The heavy aggregation is
# pure and lives in app/mcp/analysis.py; these wrappers only do I/O + shaping.


def _fetch_purchase_orders(
    deps: _Deps, *, created_after: str, supplier: str, top: int
) -> dict:
    """Bulk PO read (collection + item expand), the multi-document counterpart
    of _fetch_po_status. {ok: True, pos: [parse_po_status(...), ...]} or
    {ok: False, error}."""
    filters: list[str] = []
    if created_after:
        # API_PURCHASE_ORDER_2 is OData v4: Edm.Date literals are bare (no
        # datetime'...' wrapper). CreationDate is the field parse_po_status reads.
        filters.append(f"CreationDate ge {created_after}")
    if supplier:
        escaped = supplier.replace("'", "''")
        filters.append(f"Supplier eq '{escaped}'")
    params = {"$expand": "_PurchaseOrderItem", "$top": str(max(1, top))}
    if filters:
        params["$filter"] = " and ".join(filters)
    try:
        data = deps.client.get(
            f"{settings.sap_po_service}/PurchaseOrder",
            params=params,
            context="reading purchase orders for analysis",
        )
    except SAPRequestError as exc:
        return {"ok": False, "error": exc.user_message}
    # v4 collections return {"value": [...]}; tolerate a v2 {"d": {"results"}}
    # envelope too in case the service path is swapped to the older API.
    records = data.get("value")
    if records is None:
        records = data.get("d", {}).get("results") or []
    return {"ok": True, "pos": [parse_po_status(r) for r in records]}


def _gather_po_matches(
    deps: _Deps, *, created_after: str, supplier: str, top: int
) -> dict:
    """Fetch POs and net each against its goods receipts. Returns
    {ok: True, entries: [{po_status, match, gr_rows}, ...]} - the analysis
    module's expected input - or {ok: False, error}."""
    fetched = _fetch_purchase_orders(
        deps, created_after=created_after, supplier=supplier, top=top
    )
    if not fetched.get("ok"):
        return fetched
    entries: list[dict] = []
    for po_status in fetched["pos"]:
        number = str(po_status.get("purchase_order", "")).strip()
        if not number:
            continue
        try:
            gr_rows = fetch_goods_receipt_items(
                deps.client, settings.sap_gr_service, number
            )
        except SAPRequestError as exc:
            return {"ok": False, "error": exc.user_message}
        entries.append({
            "po_status": po_status,
            "match": compare_po_to_receipts(po_status, gr_rows),
            "gr_rows": gr_rows,
        })
    return {"ok": True, "entries": entries}


@mcp.tool()
def get_po_aging_report(
    min_days_open: int = 0,
    supplier: str = "",
    created_after: str = "",
    max_purchase_orders: int = 0,
) -> dict:
    """Open purchase orders (not fully received) ranked by how long they have
    been open. Read-only, PO vs. Goods Receipt only (no invoice leg).

    Use when the user asks what is still outstanding / not yet delivered /
    stuck / overdue across POs, rather than about one specific PO number.

    Args:
        min_days_open: Only include POs open at least this many days (e.g. 30
            for "open more than a month"). 0 = all open POs.
        supplier: Optional exact supplier code to restrict to one supplier.
        created_after: Optional ISO date 'YYYY-MM-DD'; only POs created on or
            after it are scanned (also bounds how much is read).
        max_purchase_orders: Cap on POs scanned; 0 uses the configured default.

    Returns:
        {ok: true, report: [{purchase_order, supplier, created_on, days_open,
        receipt_state, items_total, items_open, net_amount, currency}],
        count, oldest_days_open, as_of}. receipt_state is "not received" or
        "partially received"; fully-received POs are excluded by design.
    """
    try:
        deps = _deps()
    except SAPRequestError as exc:
        return {"ok": False, "error": exc.user_message}
    top = max_purchase_orders or settings.sap_analysis_max_pos
    gathered = _gather_po_matches(
        deps, created_after=created_after.strip(),
        supplier=supplier.strip(), top=top,
    )
    if not gathered.get("ok"):
        return gathered
    result = build_po_aging_report(
        gathered["entries"], date.today(),
        min_days_open=min_days_open, supplier=supplier.strip(),
    )
    return {"ok": True, **result}


@mcp.tool()
def get_supplier_performance_report(
    created_after: str = "",
    supplier: str = "",
    max_purchase_orders: int = 0,
) -> dict:
    """Per-supplier goods-receipt accuracy across recent POs, worst first.
    Read-only, PO vs. Goods Receipt only.

    Use when the user asks which suppliers deliver correctly / completely, or
    wants a supplier scorecard. Suppliers are reported by SAP code only (the
    Business Partner name API is not authorised on this tenant).

    NOTE: this covers quantity accuracy and volume, NOT on-time delivery -
    say so if the user asks specifically about timeliness.

    Args:
        created_after: Optional ISO date 'YYYY-MM-DD' lower bound on PO
            creation (also bounds how much is read).
        supplier: Optional exact supplier code to restrict to one supplier.
        max_purchase_orders: Cap on POs scanned; 0 uses the configured default.

    Returns:
        {ok: true, report: [{supplier, purchase_orders, items_total,
        items_fully_received, items_short, items_over_received,
        items_not_received, quantity_accuracy_rate}], supplier_count, note}.
        quantity_accuracy_rate is fully-received items / total items (0-1).
    """
    try:
        deps = _deps()
    except SAPRequestError as exc:
        return {"ok": False, "error": exc.user_message}
    top = max_purchase_orders or settings.sap_analysis_max_pos
    gathered = _gather_po_matches(
        deps, created_after=created_after.strip(),
        supplier=supplier.strip(), top=top,
    )
    if not gathered.get("ok"):
        return gathered
    return {"ok": True, **build_supplier_performance_report(gathered["entries"])}


@mcp.tool()
def get_po_gr_anomalies(
    variance_threshold: float = 0.0,
    created_after: str = "",
    supplier: str = "",
    max_purchase_orders: int = 0,
) -> dict:
    """Flag purchase-order lines whose receipt looks wrong: over-deliveries,
    short deliveries beyond a tolerance, or reversal/repost activity.
    Read-only, PO vs. Goods Receipt only.

    Use when the user asks to find delivery discrepancies, over/under
    deliveries, or data-quality problems across POs (not about one PO number).
    Not-yet-received lines are NOT reported here - use get_po_aging_report for
    outstanding work.

    Args:
        variance_threshold: Short-delivery sensitivity as a fraction of the
            ordered quantity (0.10 = flag receipts short by >=10%). Over-
            deliveries are always flagged. 0 uses the configured default.
        created_after: Optional ISO date 'YYYY-MM-DD' lower bound on PO creation.
        supplier: Optional exact supplier code to restrict to one supplier.
        max_purchase_orders: Cap on POs scanned; 0 uses the configured default.

    Returns:
        {ok: true, anomalies: [{purchase_order, supplier, item, material,
        ordered, received, unit, variance_pct, kind, has_reversal_activity}],
        count, variance_threshold}. kind is "over-received", "short-delivery",
        or "reversal-activity".
    """
    try:
        deps = _deps()
    except SAPRequestError as exc:
        return {"ok": False, "error": exc.user_message}
    top = max_purchase_orders or settings.sap_analysis_max_pos
    threshold = variance_threshold or settings.sap_analysis_variance_threshold
    gathered = _gather_po_matches(
        deps, created_after=created_after.strip(),
        supplier=supplier.strip(), top=top,
    )
    if not gathered.get("ok"):
        return gathered
    return {"ok": True, **detect_po_gr_anomalies(gathered["entries"], threshold)}


def main() -> None:
    logging.basicConfig(  # stderr by default - stdout belongs to the protocol
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    transport = settings.sap_mcp_transport.strip().lower()
    if transport == "streamable-http":
        mcp.settings.host = settings.sap_mcp_host
        mcp.settings.port = settings.sap_mcp_port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    main()

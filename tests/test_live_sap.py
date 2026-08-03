"""LAYER 2 - contract tests against the real S/4HANA tenant. READ-ONLY.

    pytest -m live

Deselected by any ordinary run (see conftest.pytest_collection_modifyitems),
so `pytest` alone never touches the network.

WHAT THIS LAYER IS FOR
Layer 1 proves the aggregation is right given well-formed input. It cannot
prove the OData query that produces that input works on THIS tenant - service
paths, released APIs, $filter/$expand support and authorisations are all
tenant-specific. The three analysis tools in particular have only ever run on
synthetic data: their bulk `PurchaseOrder` read with `$filter` + `$expand` is
the least-verified call in the project, and it is what answers a demo question
like "what is still outstanding?".

WHAT IT ASSERTS
Shape, never values. A live tenant's data changes; `items` being a list of
dicts carrying the keys the parser promises does not. A test that asserted
"PO 4500000002 has quantity 10" would fail the first time somebody posts a
goods receipt, and would teach the team to ignore this file.

SAFETY
Every request here is a GET. `test_no_write_is_possible_through_this_module`
statically asserts no POST/write helper is reachable from this file, so a
careless edit cannot turn a verification run into a real document being
created on a live tenant.

Each check appends a row to a verification matrix printed at the end of the
run - the dated artifact this layer exists to produce.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from app.config import settings
from app.mcp.errors import SAPRequestError

pytestmark = pytest.mark.live


# ---- matrix recording ----------------------------------------------------


def _record(request, check: str, result: str, ms: int, detail: str = "") -> None:
    """Append one row to the end-of-run verification matrix."""
    config = request.config
    if not hasattr(config, "_live_matrix"):
        config._live_matrix = []
    config._live_matrix.append({
        "check": check,
        "result": result,
        "ms": ms,
        "detail": detail,
        "tenant": _tenant_host(),
    })


def _tenant_host() -> str:
    try:
        from app.mcp.credentials import resolve_sap_credentials

        return resolve_sap_credentials(settings).base_url.replace("https://", "")
    except Exception:
        return "-"


class _Timed:
    """Times a check and records PASS/FAIL into the matrix either way, so a
    failure still shows up as a row rather than vanishing from the report."""

    def __init__(self, request, check: str):
        self.request, self.check, self.detail = request, check, ""

    def __enter__(self):
        self.started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        ms = int((time.perf_counter() - self.started) * 1000)
        if exc_type is None:
            _record(self.request, self.check, "PASS", ms, self.detail)
        else:
            reason = str(exc).splitlines()[0][:70] if exc else exc_type.__name__
            outcome = "SKIP" if exc_type is pytest.skip.Exception else "FAIL"
            _record(self.request, self.check, outcome, ms, reason)
        return False


# ---- connectivity and authorisation --------------------------------------


def test_purchase_order_service_is_reachable(request, sap_client):
    """The PO service root answers at all - isolates 'wrong service path or
    no authorisation' from 'the query is malformed', which every check below
    would otherwise report identically."""
    with _Timed(request, "PO service reachable ($metadata)") as timed:
        try:
            sap_client.get(
                f"{settings.sap_po_service}/$metadata",
                context="reading PO service metadata",
            )
        except SAPRequestError as exc:
            if exc.status_code in (401, 403):
                pytest.fail(
                    f"PO service is NOT authorised for this communication user "
                    f"(HTTP {exc.status_code}). Every PO tool is dead until the "
                    f"communication arrangement is fixed: {exc.user_message}"
                )
            raise
        timed.detail = settings.sap_po_service.split("/")[-3]


def test_goods_receipt_service_is_reachable(request, sap_client):
    with _Timed(request, "GR service reachable ($metadata)") as timed:
        sap_client.get(
            f"{settings.sap_gr_service}/$metadata",
            context="reading GR service metadata",
        )
        timed.detail = "API_MATERIAL_DOCUMENT_SRV"


def test_supplier_invoice_service_is_reachable(request, sap_client):
    """The third leg of the 3-way match. Confirmed released and read-authorised
    on 2026-07-24; this re-checks it rather than trusting a note."""
    with _Timed(request, "Supplier Invoice service reachable") as timed:
        sap_client.get(
            f"{settings.sap_supplier_invoice_service}/$metadata",
            context="reading supplier invoice metadata",
        )
        timed.detail = "API_SUPPLIERINVOICE_PROCESS_SRV"


@pytest.mark.parametrize("entity,service", [
    ("supplier", "sap_supplier_service"),
    ("material", "sap_product_service"),
    ("plant", "sap_plant_service"),
])
def test_master_data_authorisation_status(request, sap_client, entity, service):
    """Master-data lookups were 403 on this tenant as of 2026-07-20, which
    makes search_master_data a tool the chat model can call but never use.

    This check RECORDS the status rather than asserting success - the point is
    a dated answer to "is it still blocked?", not a red suite for a known
    permissions gap. If it starts passing, search_master_data became real and
    the prompt should stop hedging about it.
    """
    path = getattr(settings, service)
    with _Timed(request, f"master data: {entity}") as timed:
        try:
            sap_client.get(f"{path}/$metadata", context=f"reading {entity} metadata")
            timed.detail = "AUTHORISED (was 403 - tool is now usable)"
        except SAPRequestError as exc:
            if exc.status_code in (401, 403):
                timed.detail = f"still blocked (HTTP {exc.status_code}) - expected"
                return
            raise


# ---- the reads the tools actually make -----------------------------------


def test_single_po_read_returns_the_parsed_shape(request, sap_client):
    """get_purchase_order_status's exact call, including the item $expand."""
    from app.mcp.po import parse_po_status

    number = _a_real_po_number(sap_client)
    with _Timed(request, "single PO read + $expand items") as timed:
        data = sap_client.get(
            f"{settings.sap_po_service}/PurchaseOrder('{number}')",
            params={"$expand": "_PurchaseOrderItem"},
            context=f"reading purchase order {number}",
        )
        parsed = parse_po_status(data)

        assert parsed["purchase_order"] == number
        assert isinstance(parsed["items"], list)
        assert parsed["items"], f"PO {number} came back with no items - $expand may be ignored"
        for key in ("item", "material", "description", "quantity", "unit", "net_price"):
            assert key in parsed["items"][0], f"parsed item is missing '{key}'"
        timed.detail = f"PO {number}, {len(parsed['items'])} item(s)"


def test_goods_receipt_read_carries_the_po_reference(request, sap_client):
    """The GR->PO link is what makes the 2-way match possible at all. Without
    PurchaseOrder/PurchaseOrderItem on the rows there is no match to compute."""
    from app.mcp.gr import fetch_goods_receipt_items

    number = _a_real_po_number(sap_client)
    with _Timed(request, "goods receipt read for a PO") as timed:
        rows = fetch_goods_receipt_items(sap_client, settings.sap_gr_service, number)
        timed.detail = f"PO {number}, {len(rows)} row(s)"
        if not rows:
            pytest.skip(f"PO {number} has no goods receipts yet - nothing to shape-check")
        for key in ("PurchaseOrderItem", "QuantityInEntryUnit", "DebitCreditCode"):
            assert key in rows[0], f"GR row is missing '{key}'"


def test_bulk_po_query_with_filter_and_expand(request, sap_client):
    """THE least-verified call in the project.

    All three analysis tools (aging, supplier performance, anomalies) stand on
    this one query: a PurchaseOrder COLLECTION read with an OData v4 date
    $filter and an item $expand. It has only ever been exercised against
    synthetic data. If it 400s, three tools advertised to the chat model fail
    on their first real question.
    """
    from app.mcp.server import _fetch_purchase_orders

    class _Deps:
        client = sap_client

    with _Timed(request, "bulk PO query ($filter + $expand)") as timed:
        result = _fetch_purchase_orders(
            _Deps(), created_after="2020-01-01", supplier="", top=5
        )
        assert result.get("ok"), (
            "The bulk PO read failed - get_po_aging_report, "
            "get_supplier_performance_report and get_po_gr_anomalies are all "
            f"non-functional on this tenant: {result.get('error')}"
        )
        pos = result["pos"]
        assert isinstance(pos, list)
        timed.detail = f"{len(pos)} PO(s) returned"
        if pos:
            assert pos[0]["purchase_order"], "bulk rows parsed without a PO number"
            assert isinstance(pos[0]["items"], list), "$expand did not survive the bulk read"


def test_bulk_query_without_a_date_filter(request, sap_client):
    """The tools default to no `created_after`, so the unfiltered collection
    read must work too - some tenants require a filter and reject it."""
    from app.mcp.server import _fetch_purchase_orders

    class _Deps:
        client = sap_client

    with _Timed(request, "bulk PO query (no filter)") as timed:
        result = _fetch_purchase_orders(_Deps(), created_after="", supplier="", top=3)
        assert result.get("ok"), result.get("error")
        timed.detail = f"{len(result['pos'])} PO(s) returned"


def test_end_to_end_aging_report_on_live_data(request, sap_client):
    """The full path one chat question takes: bulk read -> per-PO goods
    receipts -> pure aggregation. Proves the tool answers, not just that the
    query parses."""
    from datetime import date

    from app.mcp.analysis import build_po_aging_report
    from app.mcp.server import _gather_po_matches

    class _Deps:
        client = sap_client

    with _Timed(request, "end-to-end aging report") as timed:
        gathered = _gather_po_matches(_Deps(), created_after="", supplier="", top=5)
        assert gathered.get("ok"), gathered.get("error")
        report = build_po_aging_report(gathered["entries"], date.today())
        assert "report" in report and "count" in report
        timed.detail = (
            f"{len(gathered['entries'])} PO(s) scanned, {report['count']} open"
        )


# ---- helpers -------------------------------------------------------------


@pytest.fixture(scope="session")
def _po_number_cache():
    return {}


def _a_real_po_number(client) -> str:
    """A PO number that exists on this tenant, discovered rather than hardcoded
    so this file keeps working when the demo data changes."""
    if getattr(_a_real_po_number, "_cached", None):
        return _a_real_po_number._cached
    try:
        data = client.get(
            f"{settings.sap_po_service}/PurchaseOrder",
            params={"$top": "1"},
            context="finding any purchase order",
        )
    except SAPRequestError as exc:
        pytest.skip(f"Could not list purchase orders: {exc.user_message}")
    records = data.get("value") or data.get("d", {}).get("results") or []
    if not records:
        pytest.skip("Tenant has no purchase orders to read")
    number = str(records[0].get("PurchaseOrder", "")).strip()
    if not number:
        pytest.skip("Purchase order row carried no document number")
    _a_real_po_number._cached = number
    return number


# The static read-only guard for this module lives in test_seams.py, NOT here.
# It must run on every ordinary `pytest`, whereas everything in this file is
# deselected unless you ask for `-m live` - a guard that only runs while you
# are already calling the tenant is a guard that fires too late.

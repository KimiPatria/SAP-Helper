"""match_invoice + find_po_line - the trust boundary of the invoice agent.

Every figure a human approves before a real SAP posting is computed here, in
plain Python, from values read elsewhere. The LLM never does this arithmetic;
it only narrates the struct. So these tests are the closest thing the project
has to a guard on "did we post the right amount".

find_po_line gets disproportionate attention because its description fallback
has broken twice, at two different seams, and both times failed *silently* -
the invoice went to human review, which looks like a policy decision rather
than a bug. See test_seams.py for the parser half of that same path.
"""

from __future__ import annotations

from factories import gr_row, invoice, po_item, po_status

from app.mcp.invoice_matching import (
    CURRENCY_MISMATCH,
    MISSING_GR,
    PRICE_VARIANCE,
    QUANTITY_VARIANCE,
    VENDOR_MISMATCH,
    Tolerances,
    find_po_line,
    match_invoice,
)

TOL = Tolerances(price_pct=0.02, price_abs=1.00, quantity_pct=0.02, quantity_abs=0.0)


def match(inv=None, status=None, rows=None, tolerances=TOL) -> dict:
    return match_invoice(
        invoice=invoice() if inv is None else inv,
        po_status=po_status() if status is None else status,
        gr_rows=[gr_row()] if rows is None else rows,
        tolerances=tolerances,
    )


# ---- the happy path ------------------------------------------------------


def test_clean_three_way_match():
    result = match()
    assert result["outcome"] == "clean"
    assert result["matched"] is True
    assert result["variances"] == []
    assert result["computed"]["received_qty"] == "10"
    assert result["computed"]["invoice_unit_price"] == "25.00"


def test_clean_match_carries_the_gr_reference_sap_requires():
    """Posting against a GR-based PO line needs the material document triple;
    an empty one here surfaces later as an opaque SAP rejection at commit."""
    computed = match()["computed"]
    assert computed["gr_material_document"] == "5000000123"
    assert computed["gr_material_document_year"] == "2026"
    assert computed["gr_material_document_item"] == "1"


# ---- tolerance arithmetic ------------------------------------------------


def test_price_within_both_floors_is_clean():
    """25.50 vs 25.00 breaches 2% but only by 0.50 - under the 1.00 absolute
    floor, so it must NOT flag. Requiring both floors is what stops rounding
    noise becoming a variance."""
    result = match(invoice(unit_price="25.50"))
    assert result["matched"] is True


def test_price_breaching_both_floors_flags():
    result = match(invoice(unit_price="30.00"))
    assert PRICE_VARIANCE in result["variance_types"]
    variance = next(v for v in result["variances"] if v["type"] == PRICE_VARIANCE)
    assert variance["delta"] == "5.00"


def test_large_absolute_but_tiny_percentage_is_clean():
    """+50 on a 10,000 unit price is 0.5% - immaterial, must not flag, even
    though it is far past the absolute floor."""
    status = po_status(items=[po_item(net_price="10000.00")])
    result = match(invoice(unit_price="10050.00"), status)
    assert result["matched"] is True


def test_quantity_variance_flags_against_received_not_ordered():
    """Invoiced 10, ordered 10, but only 8 received: the invoice is ahead of
    the delivery. Comparing against ORDERED would call this clean and approve
    payment for goods that never arrived."""
    result = match(rows=[gr_row(QuantityInEntryUnit="8")])
    assert QUANTITY_VARIANCE in result["variance_types"]
    variance = next(v for v in result["variances"] if v["type"] == QUANTITY_VARIANCE)
    assert variance["received"] == "8"
    assert variance["delta"] == "2"


def test_missing_goods_receipt_supersedes_quantity_variance():
    """With nothing received there is no third leg - reporting a quantity
    variance instead would imply a comparison that never happened."""
    result = match(rows=[])
    assert MISSING_GR in result["variance_types"]
    assert QUANTITY_VARIANCE not in result["variance_types"]


def test_reversed_receipt_reads_as_missing_gr():
    rows = [gr_row(), gr_row(DebitCreditCode="H", MaterialDocument="5000000124")]
    assert MISSING_GR in match(rows=rows)["variance_types"]


def test_currency_mismatch_flags():
    result = match(invoice(currency="USD"))  # PO is IDR
    assert CURRENCY_MISMATCH in result["variance_types"]


def test_unreadable_invoice_price_flags_rather_than_passing():
    """A price that could not be parsed must never be treated as agreeing with
    the PO - silence here would approve an unverified amount."""
    result = match(invoice(unit_price=""))
    assert PRICE_VARIANCE in result["variance_types"]


def test_missing_po_price_flags_conservatively():
    status = po_status(items=[po_item(net_price="")])
    result = match(status=status)
    assert PRICE_VARIANCE in result["variance_types"]


def test_messy_ocr_numbers_are_normalised():
    """'1,250.00' and 'IDR 1250.00' must both read as the same number - OCR
    output is not clean, and a thousands separator becoming a decimal point
    would be a 1000x posting error."""
    status = po_status(items=[po_item(net_price="1250.00")])
    result = match(invoice(unit_price="1,250.00"), status)
    assert result["matched"] is True
    assert result["computed"]["invoice_unit_price"] == "1250.00"


# ---- vendor check --------------------------------------------------------


def test_vendor_mismatch_is_not_asserted_without_a_po_supplier_name():
    """This tenant's Business Partner API is not authorised, so PO supplier
    names come back empty. Flagging a mismatch against an empty name would
    make every invoice look fraudulent."""
    result = match(invoice(vendor_name="Totally Different Corp"))
    assert VENDOR_MISMATCH not in result["variance_types"]


def test_vendor_mismatch_flags_when_the_name_is_available():
    status = po_status(supplier_name="Acme Industrial")
    result = match(invoice(vendor_name="Globex Trading"), status)
    assert VENDOR_MISMATCH in result["variance_types"]


def test_vendor_legal_suffix_differences_do_not_flag():
    status = po_status(supplier_name="Acme Industrial Ltd")
    result = match(invoice(vendor_name="ACME INDUSTRIAL GMBH"), status)
    assert VENDOR_MISMATCH not in result["variance_types"]


# ---- PO line resolution --------------------------------------------------


def test_item_number_wins_when_stated():
    items = [po_item(item="10"), po_item(item="20", material="OTHER")]
    result = find_po_line(items, "20", "")
    assert result["found"] is True
    assert result["method"] == "item-number"
    assert result["line"]["material"] == "OTHER"


def test_zero_padded_item_numbers_match_numerically():
    """SAP returns '00010' on some services and '10' on others; a string
    comparison would fail to find a line that plainly exists."""
    result = find_po_line([po_item(item="00010")], "10", "")
    assert result["found"] is True


def test_deleted_lines_are_never_matched():
    result = find_po_line([po_item(item="10", deleted=True)], "10", "")
    assert result["found"] is False


def test_description_fallback_matches_free_text():
    """The fallback must compare against the line's TEXT, not its material
    CODE - the bug that made this route unreachable for months."""
    items = [po_item(item="10", material="EGM002", description="Hex bolts M8 stainless")]
    result = find_po_line(items, "", "hex bolts stainless")
    assert result["found"] is True
    assert result["method"] == "material-description-fallback"


def test_single_shared_word_is_not_enough_to_claim_a_line():
    """One overlapping token is a coincidence. Without the floor, a single-line
    PO matches on any word at all and the pipeline 'identifies' a line it has
    not actually identified."""
    items = [po_item(description="Hex bolts M8 stainless")]
    result = find_po_line(items, "", "stainless steel washers 20mm")
    assert result["found"] is False


def test_verbatim_material_code_identifies_the_line_alone():
    items = [po_item(item="10", material="EGM002", description="")]
    result = find_po_line(items, "", "EGM002")
    assert result["found"] is True


def test_equally_scoring_lines_are_ambiguous_not_guessed():
    items = [
        po_item(item="10", material="A1", description="Hex bolts M8 stainless"),
        po_item(item="20", material="A2", description="Hex bolts M8 stainless"),
    ]
    result = find_po_line(items, "", "hex bolts stainless")
    assert result["found"] is False
    assert result["ambiguous"] is True
    assert len(result["candidates"]) == 2


def test_stated_item_number_that_matches_nothing_falls_through_to_description():
    """OCR misreading the item number must not kill the whole match when the
    description can still identify the line."""
    items = [po_item(item="10", description="Hex bolts M8 stainless")]
    result = find_po_line(items, "99", "hex bolts stainless")
    assert result["found"] is True
    assert result["method"] == "material-description-fallback"


def test_unresolvable_line_returns_candidates_for_a_human():
    items = [po_item(item="10"), po_item(item="20")]
    result = find_po_line(items, "", "")
    assert result["found"] is False
    assert len(result["candidates"]) == 2


# ---- outcomes that stop the pipeline -------------------------------------


def test_unresolved_line_yields_po_line_not_found_with_candidates():
    status = po_status(items=[po_item(item="10"), po_item(item="20")])
    result = match(invoice(po_item="", material_description=""), status)
    assert result["outcome"] == "po_line_not_found"
    assert result["matched"] is False
    assert len(result["candidates"]) == 2
    assert result["computed"] == {}


def test_ambiguous_line_is_reported_as_such():
    status = po_status(items=[
        po_item(item="10", material="A1", description="Hex bolts M8 stainless"),
        po_item(item="20", material="A2", description="Hex bolts M8 stainless"),
    ])
    result = match(invoice(po_item="", material_description="hex bolts stainless"), status)
    assert result["outcome"] == "ambiguous_po_line"


def test_every_variance_type_is_in_the_fixed_taxonomy():
    """The taxonomy is closed on purpose - the email and UI layers switch on
    it. A new category invented here would render as an unlabelled blank."""
    from app.mcp.invoice_matching import VARIANCE_TYPES

    result = match(invoice(unit_price="99.00", currency="USD"), rows=[])
    assert result["variance_types"]
    assert set(result["variance_types"]) <= VARIANCE_TYPES

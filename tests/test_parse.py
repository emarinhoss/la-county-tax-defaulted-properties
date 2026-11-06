from pathlib import Path

import pandas as pd
import pytest

from lacounty_tax.parse import _Line, assign_lines, parse_pdf

FIXTURE = Path(__file__).parent / "fixtures" / "2026B-sample-pages.pdf"


def L(y):
    return _Line(y, [{"text": f"t{y}", "x0": 0, "top": y}])


def ys(rows):
    return [[ln.y for ln in r] for r in rows]


def test_assign_lines_mirrors_around_anchor():
    # Row A: 2 lines straddling anchor 73; row B: 6 lines centred on 121,
    # whose first line (95) is closer to A's anchor than to B's.
    lines = [L(68), L(79), L(95), L(105), L(116), L(126), L(137), L(147)]
    rows, left = assign_lines(lines, [73.0, 121.0])
    assert ys(rows) == [[68, 79], [95, 105, 116, 126, 137, 147]]
    assert left == []


def test_assign_lines_single_line_rows_and_on_anchor_lines():
    lines = [L(10), L(20), L(30)]
    rows, left = assign_lines(lines, [10.0, 20.0, 30.0])
    assert ys(rows) == [[10], [20], [30]]


def test_assign_lines_reports_leftovers():
    rows, left = assign_lines([L(10), L(50)], [10.0])
    assert ys(rows) == [[10]]
    assert [ln.y for ln in left] == [50]


@pytest.fixture(scope="module")
def result():
    return parse_pdf(FIXTURE)


def test_fixture_parses_cleanly(result):
    assert result.auction == "2026B"
    assert result.warnings == []
    assert len(result.table) == 63
    assert result.table["item"].is_unique and result.table["ain"].is_unique


def row(result, item):
    return result.table.set_index("item").loc[item]


def test_improved_condo_with_wrapped_address(result):
    r = row(result, 2388)
    assert r.ain == "2024002096"
    assert r.min_bid == 41374
    assert r.improved and not r.vacant_lot
    assert r.nsb == 539
    assert r.location == "CITY LOS ANGELES"
    assert r.property_address == "22040 GAULT ST UNIT 50 LOS ANGELES CA 91303"
    assert r.zip == "91303" and pd.isna(r.zip4)


def test_long_legal_description_not_stolen_by_neighbour(result):
    r = row(result, 2390)
    assert r.legal_description.startswith("LOT COM AT MOST W COR OF LOT 6")
    assert r.legal_description.endswith("RECORDOF SURVEY AS PER BK 65 P 28 OF R S")
    assert row(result, 2388).legal_description == "*TR=38920 CONDOMINIUM*UNIT 50"
    assert r.property_address == "23760 OAKFIELD RD HIDDEN HILLS CA 91302 2412"
    assert (r.zip, r.zip4) == ("91302", "2412")


def test_vacant_lot_row(result):
    r = row(result, 2400)
    assert r.vacant_lot and not r.improved
    assert r.property_address == "VACANT LOT"
    assert pd.isna(r.zip)


def test_out_of_order_item_numbers_kept(result):
    assert 3610 in set(result.table["item"])


def test_truncated_address_without_state(result):
    r = row(result, 3665)
    assert r.property_address.endswith("ROWLAND HEIGHT 91748 2139")
    assert (r.zip, r.zip4) == ("91748", "2139")


def test_ain_is_digits_only_string(result):
    assert result.table["ain"].str.fullmatch(r"\d{10}").all()

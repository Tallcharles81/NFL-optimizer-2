"""Product identifiers (TCIN / UPC / DPCI) and schema upgrades."""

import sqlite3

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.database import Database
from app.models import Product
from app.services.product_service import ProductCreate, create_product, validation_warnings


def make(**kw):
    return ProductCreate(**{"retailer": "target", "product_name": "30th Celebration ETB", **kw})


def test_ten_digit_tcin_is_valid():
    data = make(sku="1010892076", upc="196214158801", dpci="361-00-8095")
    assert data.upc == "196214158801" and data.dpci == "361-00-8095"
    assert validation_warnings(data) == []


@pytest.mark.parametrize("sku", ["361-00-8095", "196214158801"])
def test_upc_or_dpci_in_sku_field_is_rejected(sku):
    with pytest.raises(ValidationError, match="not a TCIN"):
        make(sku=sku)


def test_bad_upc_and_dpci_rejected():
    with pytest.raises(ValidationError):
        make(sku="1010892076", dpci="3610008095")
    with pytest.raises(ValidationError):
        make(sku="1010892076", upc="12345")


async def test_alert_includes_dpci_and_upc(harness):
    pid = harness.add("A", upc="196214158801", dpci="361-00-8095")
    from app.models import InventoryStatus as S

    for status in (S.OUT_OF_STOCK, S.AVAILABLE):
        harness.sim.set_status("A", status)
        await harness.check(pid)
    extra = harness.alerts[0].extra
    assert extra["DPCI (in store)"] == "361-00-8095" and extra["UPC"] == "196214158801"


def test_old_database_gets_new_columns(tmp_path):
    path = tmp_path / "old.db"
    db = Database(f"sqlite:///{path}")
    db.create_all()
    db.dispose()
    # Simulate a database created before upc/dpci existed
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE products DROP COLUMN upc")
    con.execute("ALTER TABLE products DROP COLUMN dpci")
    con.commit()
    con.close()
    db = Database(f"sqlite:///{path}")
    db.create_all()
    with db.session() as s:
        create_product(s, make(sku="1010892076", dpci="361-00-8095"))
    with db.session() as s:
        assert s.scalar(select(Product.dpci)) == "361-00-8095"
    db.dispose()

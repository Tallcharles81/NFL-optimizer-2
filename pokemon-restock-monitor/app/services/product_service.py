"""Products, retailers and stores: creation, validation and listing."""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.database import utcnow
from app.models import InventoryState, Product, Retailer, Store
from app.retailers.registry import RETAILER_CLASSES, get_retailer_class
from app.retailers.simulated import SimulatedRetailer
from app.retailers.target import TargetMonitor

logger = logging.getLogger("products")

UPC_RE = re.compile(r"^\d{12,14}$")
DPCI_RE = re.compile(r"^\d{3}-\d{2}-\d{4}$")


class ProductCreate(BaseModel):
    retailer: str
    sku: str = Field(min_length=1, max_length=64)
    product_name: str = Field(min_length=1, max_length=300)
    product_url: str | None = None
    image_url: str | None = None
    category: str | None = "pokemon-tcg"
    store_id: str | None = None
    store_name: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    max_quantity: int | None = Field(default=None, ge=1)
    upc: str | None = None
    dpci: str | None = None
    enabled: bool = True
    poll_interval_seconds: float | None = Field(default=None, gt=0)
    accept_third_party: bool | None = None

    @field_validator("retailer")
    @classmethod
    def _retailer(cls, v: str) -> str:
        v = v.strip().lower().replace(" ", "_").replace("-", "_")
        try:
            get_retailer_class(v)
        except KeyError as exc:
            raise ValueError(str(exc).strip('"')) from None
        return v

    @field_validator("sku", "product_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("product_url", "image_url", "store_id", "store_name", "city", "state", "zip_code",
                     "category", "upc", "dpci", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    @field_validator("product_url", "image_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v and not v.startswith(("https://", "http://")):
            raise ValueError("URL must start with https://")
        return v

    @field_validator("upc")
    @classmethod
    def _upc(cls, v: str | None) -> str | None:
        if v and not UPC_RE.match(v):
            raise ValueError(f"UPC {v!r} should be 12-14 digits")
        return v

    @field_validator("dpci")
    @classmethod
    def _dpci(cls, v: str | None) -> str | None:
        if v and not DPCI_RE.match(v):
            raise ValueError(f"DPCI {v!r} should look like 361-00-8095")
        return v

    @model_validator(mode="after")
    def _target_sku_is_tcin(self):
        # A UPC or DPCI in the SKU field would silently monitor the wrong URL.
        if self.retailer == "target":
            if DPCI_RE.match(self.sku):
                raise ValueError(f"{self.sku} is a Target DPCI (in-store number), not a TCIN. Pass it as "
                                 "--dpci and use the number after /A- in the target.com URL as --sku.")
            if UPC_RE.match(self.sku) and len(self.sku) >= 12:
                raise ValueError(f"{self.sku} looks like a UPC barcode, not a TCIN. Pass it as --upc and "
                                 "use the number after /A- in the target.com URL as --sku.")
        return self


class ProductUpdate(BaseModel):
    product_name: str | None = None
    product_url: str | None = None
    image_url: str | None = None
    enabled: bool | None = None
    max_quantity: int | None = None
    upc: str | None = None
    dpci: str | None = None
    poll_interval_seconds: float | None = None
    accept_third_party: bool | None = None


def ensure_retailers(session: Session, include_simulated: bool = False) -> None:
    existing = {r.slug for r in session.scalars(select(Retailer))}
    for slug, cls in RETAILER_CLASSES.items():
        if cls is SimulatedRetailer and not include_simulated:
            continue
        if slug not in existing:
            session.add(Retailer(slug=slug, name=cls.display_name, enabled=True))
    session.flush()


def get_retailer(session: Session, slug: str, create: bool = True) -> Retailer:
    slug = slug.lower()
    row = session.scalar(select(Retailer).where(Retailer.slug == slug))
    if row is None:
        if not create:
            raise LookupError(f"Retailer {slug!r} not found")
        cls = get_retailer_class(slug)
        row = Retailer(slug=slug, name=cls.display_name, enabled=True)
        session.add(row)
        session.flush()
    return row


def add_store(session: Session, retailer_slug: str, store_id: str, name: str | None = None,
              city: str | None = None, state: str | None = None, zip_code: str | None = None,
              source: str = "manual") -> Store:
    retailer = get_retailer(session, retailer_slug)
    store = session.scalar(select(Store).where(Store.retailer_id == retailer.id, Store.store_id == store_id))
    if store is None:
        store = Store(retailer_id=retailer.id, store_id=store_id, name=name or f"Store {store_id}",
                      city=city, state=state, zip_code=zip_code, source=source)
        session.add(store)
    else:
        store.name = name or store.name
        store.city = city or store.city
        store.state = state or store.state
        store.zip_code = zip_code or store.zip_code
    session.flush()
    return store


def sync_config_stores(session: Session, settings: Settings) -> int:
    count = 0
    for cfg in settings.configured_stores():
        try:
            get_retailer_class(cfg.retailer)
        except KeyError:
            logger.error("Ignoring store for unknown retailer %r", cfg.retailer)
            continue
        add_store(session, cfg.retailer, cfg.store_id, cfg.name, cfg.city, cfg.state, cfg.zip_code, "config")
        count += 1
    return count


def list_stores(session: Session, retailer_slug: str | None = None) -> list[Store]:
    q = select(Store).options(selectinload(Store.retailer)).order_by(Store.retailer_id, Store.name)
    if retailer_slug:
        q = q.join(Retailer).where(Retailer.slug == retailer_slug.lower())
    return list(session.scalars(q))


def validation_warnings(data: ProductCreate) -> list[str]:
    warnings = []
    cls = get_retailer_class(data.retailer)
    if data.retailer == "target":
        if not TargetMonitor.is_valid_tcin(data.sku):
            from_url = TargetMonitor.tcin_from_url(data.product_url)
            warnings.append(
                f"Target SKUs are numeric TCINs (e.g. 1010892076). {data.sku!r} doesn't look like one"
                + (f"; the URL contains TCIN {from_url}" if from_url else "")
            )
    elif cls is not SimulatedRetailer and not data.product_url:
        warnings.append(f"{cls.display_name} needs a full product URL to be checked.")
    if data.store_id and not cls.supports_store_inventory:
        warnings.append(
            f"{cls.display_name} has no permitted store-level inventory source implemented; this store "
            "product will report UNKNOWN (online availability is never reported as in-store)."
        )
    return warnings


def create_product(session: Session, data: ProductCreate) -> Product:
    retailer = get_retailer(session, data.retailer)
    store_fields = {}
    if data.store_id:
        store = session.scalar(select(Store).where(Store.retailer_id == retailer.id,
                                                   Store.store_id == data.store_id))
        if store is None:
            store = add_store(session, data.retailer, data.store_id, data.store_name, data.city,
                              data.state, data.zip_code)
        store_fields = dict(
            store_name=data.store_name or store.name,
            city=data.city or store.city,
            state=data.state or store.state,
            zip_code=data.zip_code or store.zip_code,
        )
    dup = session.scalar(select(Product).where(
        Product.retailer_id == retailer.id, Product.sku == data.sku,
        Product.store_id.is_(None) if not data.store_id else Product.store_id == data.store_id))
    if dup is not None:
        raise ValueError(f"Product {data.retailer}/{data.sku} at {dup.location_label} already exists (id={dup.id})")

    product = Product(
        retailer_id=retailer.id,
        sku=data.sku,
        product_name=data.product_name,
        product_url=data.product_url,
        image_url=data.image_url,
        category=data.category,
        enabled=data.enabled,
        max_quantity=data.max_quantity,
        upc=data.upc,
        dpci=data.dpci,
        store_id=data.store_id,
        store_name=store_fields.get("store_name", data.store_name),
        city=store_fields.get("city", data.city),
        state=store_fields.get("state", data.state),
        zip_code=store_fields.get("zip_code", data.zip_code),
        poll_interval_seconds=data.poll_interval_seconds,
        accept_third_party=data.accept_third_party,
    )
    session.add(product)
    session.flush()
    session.add(InventoryState(product_id=product.id, next_check_at=utcnow()))
    session.flush()
    return product


def update_product(session: Session, product_id: int, data: ProductUpdate) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise LookupError(f"Product {product_id} not found")
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(product, key, value)
    if data.enabled and product.state_row is not None:
        product.state_row.next_check_at = utcnow()
    return product


def delete_product(session: Session, product_id: int) -> None:
    product = session.get(Product, product_id)
    if product is None:
        raise LookupError(f"Product {product_id} not found")
    session.delete(product)


def list_products(session: Session) -> list[Product]:
    return list(session.scalars(
        select(Product)
        .options(selectinload(Product.retailer), selectinload(Product.state_row))
        .order_by(Product.product_name, Product.store_id)
    ))


def import_catalog(session: Session, path: str, retailer: str = "target", max_quantity: int | None = None,
                   locations: list[dict] | None = None, enabled: bool = True) -> dict:
    """Idempotently import products from a CSV (name,tcin[,upc,dpci,url,max_quantity]).

    Returns {"added": [...], "existing": n, "errors": [...]}.
    """
    import csv

    result = {"added": [], "existing": 0, "errors": []}
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for raw in rows:
        row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        sku = row.get("tcin") or row.get("sku")
        if not sku:
            result["errors"].append(f"{row.get('name')}: no TCIN")
            continue
        mq = row.get("max_quantity") or max_quantity
        for loc in locations or [{}]:
            try:
                data = ProductCreate(retailer=retailer, sku=sku, product_name=row.get("name") or sku,
                                     product_url=row.get("url") or None, upc=row.get("upc") or None,
                                     dpci=row.get("dpci") or None, max_quantity=int(mq) if mq else None,
                                     enabled=enabled, **loc)
            except ValidationError as exc:
                result["errors"].append(f"{row.get('name')}: {exc.errors()[0]['msg']}")
                continue
            try:
                with session.begin_nested():
                    product = create_product(session, data)
            except ValueError:
                result["existing"] += 1
                continue
            result["added"].append(product)
    return result

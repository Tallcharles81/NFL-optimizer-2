"""Notification channel interface and the message model."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class NotificationError(Exception):
    def __init__(self, message: str, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass
class AlertMessage:
    kind: str  # restock | system | test
    title: str
    product_name: str | None = None
    retailer: str | None = None
    store: str | None = None
    status: str | None = None
    sku: str | None = None
    detected: str | None = None
    url: str | None = None
    image_url: str | None = None
    extra: dict[str, str] = field(default_factory=dict)
    text: str | None = None
    is_simulation: bool = False
    # Additional link buttons shown after OPEN PRODUCT, as (label, url).
    links: list[tuple[str, str]] = field(default_factory=list)

    def core_fields(self) -> list[tuple[str, str]]:
        pairs = [
            ("Product", self.product_name),
            ("Retailer", self.retailer),
            ("Store", self.store),
            ("Status", self.status),
            ("SKU", self.sku),
            ("Detected", self.detected),
        ]
        return [(k, v) for k, v in pairs if v]

    def as_text(self) -> str:
        lines = [self.title, ""]
        if self.text:
            lines += [self.text, ""]
        for label, value in [*self.core_fields(), *self.extra.items()]:
            lines += [f"{label}:", str(value), ""]
        if self.url:
            lines += ["OPEN PRODUCT:", self.url]
        for label, link in self.links:
            lines += ["", f"{label}:", link]
        return "\n".join(lines).rstrip()


class Notifier(ABC):
    name: str = "base"

    @abstractmethod
    async def send(self, message: AlertMessage) -> None:
        """Deliver the message or raise NotificationError."""

    async def aclose(self) -> None:  # pragma: no cover - trivial
        return None

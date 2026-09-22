"""Optional email notifications over SMTP (credentials from .env only)."""

from __future__ import annotations

import asyncio
import html
import smtplib
from email.message import EmailMessage

from app.config import Settings
from app.notifications.base import AlertMessage, NotificationError, Notifier


class EmailNotifier(Notifier):
    name = "email"

    def __init__(self, settings: Settings):
        if not (settings.smtp_host and settings.email_from and settings.email_to):
            raise ValueError("EMAIL_ENABLED=true requires SMTP_HOST, EMAIL_FROM and EMAIL_TO")
        self.s = settings

    def build(self, msg: AlertMessage) -> EmailMessage:
        em = EmailMessage()
        prefix = "[SIMULATION] " if msg.is_simulation else ""
        subject = f"{prefix}{msg.title}"
        if msg.product_name:
            subject += f" - {msg.product_name}"
        em["Subject"] = subject[:200]
        em["From"] = self.s.email_from
        em["To"] = self.s.email_to
        em.set_content(msg.as_text())
        rows = "".join(
            f"<tr><th align='left'>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>"
            for k, v in [*msg.core_fields(), *msg.extra.items()]
        )
        button = (f"<p><a href='{html.escape(msg.url)}' style='background:#e3350d;color:#fff;padding:10px 16px;"
                  f"border-radius:6px;text-decoration:none'>OPEN PRODUCT</a></p>") if msg.url else ""
        button += "".join(f"<p><a href='{html.escape(link)}'>{html.escape(label)}</a></p>" for label, link in msg.links)
        body = f"<h2>{html.escape(prefix + msg.title)}</h2>"
        if msg.text:
            body += f"<p>{html.escape(msg.text)}</p>"
        em.add_alternative(f"{body}<table>{rows}</table>{button}", subtype="html")
        return em

    def _send_sync(self, em: EmailMessage) -> None:
        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=15) as smtp:
            if self.s.smtp_use_tls:
                smtp.starttls()
            if self.s.smtp_username and self.s.smtp_password:
                smtp.login(self.s.smtp_username, self.s.smtp_password)
            smtp.send_message(em)

    async def send(self, message: AlertMessage) -> None:
        try:
            await asyncio.to_thread(self._send_sync, self.build(message))
        except (smtplib.SMTPException, OSError) as exc:
            raise NotificationError(f"Email failed: {type(exc).__name__}: {exc}", retryable=True) from None

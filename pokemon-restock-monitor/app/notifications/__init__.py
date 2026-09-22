"""Notification channels."""

from __future__ import annotations

import logging

from app.config import Settings
from app.notifications.base import AlertMessage, NotificationError, Notifier  # noqa: F401

logger = logging.getLogger("notify")


def build_channels(settings: Settings) -> list[Notifier]:
    """Instantiate every channel that is configured in the environment."""
    from app.notifications.console import ConsoleNotifier
    from app.notifications.discord import DiscordNotifier
    from app.notifications.email import EmailNotifier
    from app.notifications.telegram import TelegramNotifier

    channels: list[Notifier] = []
    if settings.discord_webhook_url:
        channels.append(DiscordNotifier(settings.discord_webhook_url, settings.discord_mention,
                                        settings.discord_use_buttons))
    if settings.telegram_bot_token and settings.telegram_chat_id:
        channels.append(TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id))
    if settings.email_enabled:
        try:
            channels.append(EmailNotifier(settings))
        except ValueError as exc:
            logger.error("Email notifications disabled: %s", exc)
    if settings.notify_console or not channels:
        channels.append(ConsoleNotifier())
    return channels

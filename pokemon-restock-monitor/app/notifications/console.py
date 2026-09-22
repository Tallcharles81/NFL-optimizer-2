"""Logs alerts to stdout. Always available; handy for simulation and debugging."""

import logging

from app.notifications.base import AlertMessage, Notifier

logger = logging.getLogger("notify.console")


class ConsoleNotifier(Notifier):
    name = "console"

    def __init__(self):
        self.sent: list[AlertMessage] = []

    async def send(self, message: AlertMessage) -> None:
        self.sent.append(message)
        border = "=" * 60
        logger.warning("\n%s\n%s\n%s", border, message.as_text(), border)

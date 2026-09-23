#!/usr/bin/env python
"""Send a test notification through every configured channel (Discord, Telegram, email, console).

  python scripts/test_notifications.py [--retailer walmart]
"""

import argparse
import asyncio
import sys

import _bootstrap  # noqa: F401

from app.config import get_settings
from app.database import init_database
from app.services.notification_service import NotificationService
from app.utils.logging import configure_logging


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--retailer", default="target", choices=["target", "walmart"],
                        help="which retailer's alert to imitate")
    args = parser.parse_args()
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    db = init_database(settings.database_url)
    svc = NotificationService(settings, db)
    names = [c.name for c in svc.channels]
    print(f"Configured channels: {', '.join(names)}")
    if names == ["console"]:
        print("Only the console channel is active. Set DISCORD_WEBHOOK_URL (and/or Telegram/email) in .env.")
    report = await svc.send_test_message(args.retailer)
    await svc.aclose()
    for ch in report.sent:
        print(f"  OK      {ch}")
    for ch, err in report.failed.items():
        print(f"  FAILED  {ch}: {err}")
    return 1 if report.failed or not report.sent else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

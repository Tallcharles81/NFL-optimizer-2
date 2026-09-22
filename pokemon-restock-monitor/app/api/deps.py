"""Shared FastAPI dependencies."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.runtime import Runtime

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
_basic = HTTPBasic(auto_error=False)


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def get_session(runtime: Runtime = Depends(get_runtime)) -> Iterator[Session]:
    with runtime.db.session() as s:
        yield s


def require_auth(request: Request, creds: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    settings = request.app.state.runtime.settings
    if not settings.dashboard_password:
        return
    ok = creds is not None and secrets.compare_digest(creds.username, settings.dashboard_username) \
        and secrets.compare_digest(creds.password, settings.dashboard_password)
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required",
                            headers={"WWW-Authenticate": "Basic"})


def _localtime(dt: datetime | None, fmt: str = "%Y-%m-%d %-I:%M:%S %p") -> str:
    if dt is None:
        return "—"
    tz = ZoneInfo(templates.env.globals.get("tz_name", "UTC"))
    return dt.astimezone(tz).strftime(fmt)


def _ago(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 0:
        secs = -secs
        return f"in {int(secs)}s" if secs < 90 else (f"in {int(secs // 60)}m" if secs < 5400 else f"in {secs / 3600:.1f}h")
    if secs < 90:
        return f"{int(secs)}s ago"
    if secs < 5400:
        return f"{int(secs // 60)}m ago"
    if secs < 172800:
        return f"{secs / 3600:.1f}h ago"
    return f"{secs / 86400:.1f}d ago"


def _ms(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value} ms" if value < 10_000 else f"{value / 1000:.1f} s"


templates.env.filters["localtime"] = _localtime
templates.env.filters["ago"] = _ago
templates.env.filters["ms"] = _ms

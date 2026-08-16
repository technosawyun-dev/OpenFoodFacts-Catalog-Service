from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from app.config import settings


async def require_api_key(x_api_key: str = Header(default="")) -> None:
    if not secrets.compare_digest(x_api_key, settings.OFF_SERVICE_API_KEY):
        raise HTTPException(status_code=401, detail="invalid or missing API key")

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from app.auth import require_api_key
from app.lookup import lookup_openfoodfacts, parquet_present

app = FastAPI(title="OpenFoodFacts Catalog Service")


@app.get("/health")
async def health() -> dict:
    # No API key required — this is what the Docker healthcheck and
    # Cloudflare Tunnel origin check hit.
    return {"status": "ok", "parquet_present": parquet_present()}


@app.get("/lookup/{barcode}", dependencies=[Depends(require_api_key)])
async def lookup(barcode: str) -> JSONResponse:
    result = await lookup_openfoodfacts(barcode)
    if result is None:
        return JSONResponse(status_code=404, content={"detail": "not found"})
    return JSONResponse(status_code=200, content=result)

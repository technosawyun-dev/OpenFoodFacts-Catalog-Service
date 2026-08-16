from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TypedDict

import duckdb
from starlette.concurrency import run_in_threadpool

from app.config import settings

# Ported from POS_System_For_all_businesses's original
# backend/app/services/openfoodfacts_lookup.py — same query, same cleaning
# rules, same image-URL derivation, so results are identical to what that
# app used to compute locally before this lookup moved onto its own service.

_MAX_TEXT_LEN = 255


class OFFLookupResult(TypedDict):
    name: str | None
    brands: str | None
    categories: str | None
    image_url: str | None


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    stripped = value.replace("\x00", "").strip()[:_MAX_TEXT_LEN]
    if not stripped or stripped.lower() == "null":
        return None
    return stripped


def _off_image_url(barcode: str, key: str, rev: int, size: str = "400") -> str:
    """https://images.openfoodfacts.org/images/products/{path}/{key}.{rev}.{size}.jpg

    {path} = barcode zero-padded to 13 digits, first 9 digits split into groups
    of 3, remaining 4 digits as the final segment.
    e.g. 3017620422003 -> 301/762/042/2003
    """
    padded = barcode.rjust(13, "0")
    head, tail = padded[:9], padded[9:]
    groups = [head[i : i + 3] for i in range(0, 9, 3)] + [tail]
    return f"https://images.openfoodfacts.org/images/products/{'/'.join(groups)}/{key}.{rev}.{size}.jpg"


def _pick_front_image(barcode: str, images: list[dict] | None, preferred_lang: str | None) -> str | None:
    if not images:
        return None
    fronts = {
        im["key"]: im
        for im in images
        if im.get("key", "").startswith("front_") and im.get("imgid") is not None and im.get("rev") is not None
    }
    if not fronts:
        return None
    for key in filter(None, [f"front_{preferred_lang}" if preferred_lang else None, "front_en"]):
        if key in fronts:
            im = fronts[key]
            return _off_image_url(barcode, key, im["rev"])
    key = sorted(fronts)[0]
    return _off_image_url(barcode, key, fronts[key]["rev"])


def _query_parquet_sync(barcode: str) -> OFFLookupResult | None:
    """Blocking DuckDB query. Must only be called via run_in_threadpool."""
    path = Path(settings.OFF_PARQUET_PATH)
    if not path.is_file():
        return None

    con = duckdb.connect()
    try:
        con.execute(f"PRAGMA memory_limit='{settings.OFF_LOOKUP_MEMORY_LIMIT_MB}MB'")
        row = con.execute(
            "SELECT code, product_name, brands, categories, images, lang "
            "FROM read_parquet(?) WHERE code = ? LIMIT 1",
            [str(path), barcode],
        ).fetchone()
    finally:
        con.close()

    if row is None:
        return None

    _code, product_name, brands, categories, images, lang = row

    name = None
    if product_name:
        by_lang = {e["lang"]: e["text"] for e in product_name if e.get("text")}
        name = by_lang.get("main") or by_lang.get("en") or next(iter(by_lang.values()), None)
        name = _clean_text(name)

    category_name = categories.split(",")[0].strip() if categories else None
    return {
        "name": name,
        "brands": _clean_text(brands),
        "categories": _clean_text(category_name),
        "image_url": _pick_front_image(barcode, images, lang),
    }


async def lookup_openfoodfacts(barcode: str) -> OFFLookupResult | None:
    """Never raises: a missing/corrupt/slow Parquet file degrades to "not
    found" rather than a 500 — the caller (business VPS) already treats any
    non-200 response as a clean miss.
    """
    try:
        return await asyncio.wait_for(
            run_in_threadpool(_query_parquet_sync, barcode),
            timeout=settings.OFF_LOOKUP_TIMEOUT_SECONDS,
        )
    except Exception:
        return None


def parquet_present() -> bool:
    return Path(settings.OFF_PARQUET_PATH).is_file()

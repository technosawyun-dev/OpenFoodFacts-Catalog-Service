from __future__ import annotations

import asyncio
import threading
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


def _row_to_result(row: tuple) -> OFFLookupResult:
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
        "image_url": _pick_front_image(_code, images, lang),
    }


# --- Fast path: a persistent, indexed connection built by refresh/build_index.py ---
#
# The old approach ran `read_parquet(path) WHERE code = ?` fresh on every
# request. DuckDB can't prune row groups on an unsorted, un-indexed 7GB+ file
# for a point lookup, so that was close to a full-file scan every time —
# measured at ~1-2.5s per barcode against the real dataset. An on-disk table
# with an ART index on `code`, opened once and reused, does the same lookup
# in ~1-6ms.
#
# Reopened whenever the file's mtime changes, so refresh/build_index.py's
# atomic monthly swap (tmp file + rename, same pattern as the parquet itself)
# is picked up without restarting off-api — no reader ever sees a partial
# swap, and there's no downtime gap either.
_index_lock = threading.Lock()
_index_con: duckdb.DuckDBPyConnection | None = None
_index_mtime: float | None = None


def _get_index_connection() -> duckdb.DuckDBPyConnection | None:
    path = Path(settings.OFF_INDEX_DB_PATH)
    if not path.is_file():
        return None
    mtime = path.stat().st_mtime
    global _index_con, _index_mtime
    with _index_lock:
        if _index_con is None or mtime != _index_mtime:
            if _index_con is not None:
                _index_con.close()
            con = duckdb.connect(str(path), read_only=True)
            con.execute(f"PRAGMA memory_limit='{settings.OFF_LOOKUP_MEMORY_LIMIT_MB}MB'")
            _index_con = con
            _index_mtime = mtime
        return _index_con


def _query_index_sync(barcode: str) -> OFFLookupResult | None:
    """Blocking DuckDB query against the pre-built index. Must only be
    called via run_in_threadpool. Uses a cursor (cheap, independent execution
    context) off the shared connection so concurrent requests on different
    threadpool threads don't serialize on a single connection."""
    con = _get_index_connection()
    cursor = con.cursor()
    try:
        row = cursor.execute(
            "SELECT code, product_name, brands, categories, images, lang "
            "FROM food WHERE code = ? LIMIT 1",
            [barcode],
        ).fetchone()
    finally:
        cursor.close()
    return _row_to_result(row) if row is not None else None


# --- Slow-path fallback: only used if the index hasn't been built yet
# (e.g. brand-new deploy before the first refresh has run). Same query
# duckdb ran before this file's indexed-lookup fast path existed. ---
def _query_parquet_sync(barcode: str) -> OFFLookupResult | None:
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

    return _row_to_result(row) if row is not None else None


def _candidate_codes(barcode: str) -> list[str]:
    """OpenFoodFacts stores every barcode normalized to GTIN-13 (zero-padded),
    but a scanner reading a US/Canadian product's physical UPC-A barcode
    reports it as 12 raw digits — no leading zero. An exact-match lookup on
    the raw 12-digit form then misses real, complete OFF data (name, brand,
    photo) for barcodes that do exist, just under their padded form. Try the
    scanned code first (covers EAN-13/EAN-8/anything already normalized),
    then the zero-padded-to-13 form if that's actually different.
    """
    candidates = [barcode]
    if barcode.isdigit() and len(barcode) < 13:
        padded = barcode.zfill(13)
        if padded != barcode:
            candidates.append(padded)
    return candidates


def _query_one(code: str) -> OFFLookupResult | None:
    if _get_index_connection() is not None:
        return _query_index_sync(code)
    return _query_parquet_sync(code)


def _query_sync(barcode: str) -> OFFLookupResult | None:
    for code in _candidate_codes(barcode):
        result = _query_one(code)
        if result is not None:
            return result
    return None


async def lookup_openfoodfacts(barcode: str) -> OFFLookupResult | None:
    """Never raises: a missing/corrupt/slow index or Parquet file degrades to
    "not found" rather than a 500 — the caller (business VPS) already treats
    any non-200 response as a clean miss.
    """
    try:
        return await asyncio.wait_for(
            run_in_threadpool(_query_sync, barcode),
            timeout=settings.OFF_LOOKUP_TIMEOUT_SECONDS,
        )
    except Exception:
        return None


def parquet_present() -> bool:
    return Path(settings.OFF_PARQUET_PATH).is_file()


def index_present() -> bool:
    return Path(settings.OFF_INDEX_DB_PATH).is_file()

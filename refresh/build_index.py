"""Parquet -> indexed DuckDB database, for fast per-barcode lookups.

api/app/lookup.py's old approach ran `read_parquet(...) WHERE code = ?` fresh
on every request — DuckDB has no way to prune row groups on an unsorted,
un-indexed 7GB+ file for a point lookup, so that was a near-full-file scan
every time (~1-2.5s per barcode). This script instead builds a real on-disk
DuckDB table with an ART index on `code` once, so the running service just
opens it read-only and does an index lookup (~1-6ms, confirmed by timing
both approaches against the real dataset).

Deliberately NOT built inside the off-api container: building needs several
GB of scratch memory (DuckDB's index/table build, not the final file size),
which would blow well past off-api's serving-time memory limit. Same
reasoning as export_catalog_csv.py and the parquet download itself — this
runs on the host via cron, off-api only ever reads the finished, already-low-memory
result.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import duckdb

# Generous — this is a one-shot offline build, not the serving-time budget.
# api/app/lookup.py opens the finished file with a much smaller limit.
BUILD_MEMORY_MB = int(os.environ.get("OFF_REFRESH_DUCKDB_MEMORY_MB", "4096"))


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def build_index(parquet_path: Path, db_path: Path) -> int:
    if db_path.exists():
        db_path.unlink()

    con = duckdb.connect(str(db_path))
    try:
        # preserve_insertion_order=false lets DuckDB stream/sort without
        # holding the whole build in memory at once — without it, this OOMs
        # at BUILD_MEMORY_MB well before finishing on the real 4.6M-row file.
        con.execute("SET preserve_insertion_order=false")
        con.execute(f"PRAGMA memory_limit='{BUILD_MEMORY_MB}MB'")
        # Same 6 columns api/app/lookup.py actually reads — no point indexing
        # (and paying disk for) the ~100 OFF columns nothing here ever queries.
        con.execute(
            "CREATE TABLE food AS "
            "SELECT code, product_name, brands, categories, images, lang "
            "FROM read_parquet(?)",
            [str(parquet_path)],
        )
        # Non-unique: real OFF data has duplicate barcodes; lookup.py already
        # does LIMIT 1 and doesn't care which duplicate it gets.
        con.execute("CREATE INDEX idx_code ON food(code)")
        con.execute("CHECKPOINT")
        row_count = con.execute("SELECT COUNT(*) FROM food").fetchone()[0]
    finally:
        con.close()
    return row_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", required=True, help="Path to food.parquet")
    parser.add_argument("--out", required=True, help="Output .duckdb path")
    args = parser.parse_args()

    parquet_path = Path(args.parquet)
    if not parquet_path.is_file():
        log(f"ERROR: parquet file not found at {parquet_path}")
        return 1

    out_path = Path(args.out)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")

    log(f"Building indexed DB from {parquet_path} (DuckDB build memory cap {BUILD_MEMORY_MB}MB)")
    t0 = time.time()
    row_count = build_index(parquet_path, tmp_path)
    log(f"Indexed {row_count} rows in {time.time() - t0:.1f}s")

    # Atomic swap — off-api's mtime-watch (see lookup.py) picks up the new
    # file on its next request; readers of --out never see a partial file.
    # DuckDB may also write a "<path>.wal" beside the db file; there's none
    # to carry over here since we CHECKPOINT'd before closing above.
    tmp_path.replace(out_path)
    log(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

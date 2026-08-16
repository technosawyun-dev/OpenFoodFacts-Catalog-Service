"""Parquet -> lean catalog CSV, for the business VPS's off_refresh_import.py --csv.

Same DuckDB export query as POS_System_For_all_businesses's original
backend/scripts/off_refresh_import.py (export_staging_csv), so the CSV this
produces is byte-for-byte the same shape that script already knows how to
import. The only difference is this box can afford more memory and real
parallelism (no small-VPS pacing needed) since it isn't also running the
live checkout/API traffic that off_refresh_import.py had to stay gentle around.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import duckdb

DUCKDB_MEMORY_MB = int(os.environ.get("OFF_REFRESH_DUCKDB_MEMORY_MB", "4096"))


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def export_catalog_csv(parquet_path: Path, csv_path: Path) -> int:
    con = duckdb.connect()
    con.execute(f"PRAGMA memory_limit='{DUCKDB_MEMORY_MB}MB'")

    # Same cleaning rules as api/app/lookup.py's _clean_text: strip NUL bytes
    # (Postgres can never store them) and the literal string "null" some OFF
    # rows use in place of a true empty value, truncate to 255 chars (every
    # target column on the business VPS side is VARCHAR(255)).
    sql = f"""
        COPY (
            SELECT
                row_number() OVER () AS rn,
                substr(replace(code, chr(0), ''), 1, 255) AS barcode,
                substr(replace(
                    COALESCE(
                        list_filter(product_name, x -> x.lang = 'main')[1].text,
                        list_filter(product_name, x -> x.lang = 'en')[1].text,
                        product_name[1].text
                    ), chr(0), ''
                ), 1, 255) AS name,
                NULLIF(NULLIF(substr(TRIM(replace(split_part(categories, ',', 1), chr(0), '')), 1, 255), ''), 'null') AS category_name,
                NULLIF(NULLIF(substr(TRIM(replace(brands, chr(0), '')), 1, 255), ''), 'null') AS brand_name
            FROM read_parquet(?)
            WHERE code IS NOT NULL AND length(code) > 0
              AND product_name IS NOT NULL AND len(product_name) > 0
            QUALIFY ROW_NUMBER() OVER (PARTITION BY code) = 1
        ) TO '{csv_path}' (FORMAT CSV, HEADER)
    """
    con.execute(sql, [str(parquet_path)])
    con.close()

    with open(csv_path, "r", encoding="utf-8") as f:
        return sum(1 for _ in f) - 1  # minus header


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", required=True, help="Path to food.parquet")
    parser.add_argument("--out", required=True, help="Output CSV path")
    args = parser.parse_args()

    parquet_path = Path(args.parquet)
    if not parquet_path.is_file():
        log(f"ERROR: parquet file not found at {parquet_path}")
        return 1

    out_path = Path(args.out)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")

    log(f"Exporting catalog fields from {parquet_path} (DuckDB memory cap {DUCKDB_MEMORY_MB}MB)")
    t0 = time.time()
    row_count = export_catalog_csv(parquet_path, tmp_path)
    log(f"Exported {row_count} rows in {time.time() - t0:.1f}s")

    tmp_path.replace(out_path)  # atomic swap — readers of --out never see a partial file
    log(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

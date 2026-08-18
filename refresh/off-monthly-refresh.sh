#!/bin/sh
set -eu

# Monthly OpenFoodFacts refresh, meant to run from cron on the shared VPS
# host (not in a container — keeps its memory use out of off-api's cgroup
# limit, same reasoning as the original business-VPS version of this script).
#
# Steps, each cheap-first:
#   1. HEAD request (~1KB) to check whether OpenFoodFacts' export actually
#      changed since last run. If not, exit immediately.
#   2. Bandwidth-throttled download to a .tmp file (off-api keeps serving off
#      the OLD file this whole time — zero downtime).
#   3. Atomic rename over the live file only after the download is verified
#      complete — off-api never needs a restart.
#   4. Export a lean catalog.csv from the new parquet (export_catalog_csv.py)
#      and atomically publish it at OFF_CATALOG_PUBLISH_DIR/catalog-latest.csv.
#      Deliberately a *fixed* location independent of wherever this repo is
#      checked out, because it doubles as the SFTP path the business VPS's
#      off-monthly-refresh.sh pulls from — that script hardcodes the relative
#      path "off-catalog/catalog-latest.csv" under the appbox SFTP account's
#      home, so this must resolve to the same place regardless of where
#      OpenFoodFacts-Catalog-Service itself lives on disk.
#   5. Rebuild the indexed food_index.duckdb (build_index.py) from the new
#      parquet and atomically swap it in — this is what api/app/lookup.py
#      actually queries per-request (~1-6ms vs ~1-2.5s scanning the raw
#      parquet). Needs real scratch memory (see build_index.py), which is
#      exactly why this whole script runs on the host and not in a
#      container: off-api's own memory limit only has to cover *serving*
#      the finished, already-built index, never building it.

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG="${REPO_DIR}/logs/off-monthly-refresh.log"
STATE_FILE="${REPO_DIR}/logs/.off-refresh-etag"

OFF_URL="https://huggingface.co/datasets/openfoodfacts/product-database/resolve/main/food.parquet?download=true"
DOWNLOAD_RATE_LIMIT="${OFF_REFRESH_RATE_LIMIT:-10M}"

PARQUET_DIR="${REPO_DIR}/data/openfoodfacts"
PARQUET_PATH="${PARQUET_DIR}/food.parquet"
TMP_PATH="${PARQUET_PATH}.downloading"
INDEX_DB_PATH="${PARQUET_DIR}/food_index.duckdb"

CATALOG_DIR="${OFF_CATALOG_PUBLISH_DIR:-${HOME}/off-catalog}"
CATALOG_CSV="${CATALOG_DIR}/catalog-latest.csv"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" | tee -a "$LOG"
}

mkdir -p "${REPO_DIR}/logs" "$PARQUET_DIR" "$CATALOG_DIR"

if [ -f "${REPO_DIR}/.env" ]; then
    set -a
    . "${REPO_DIR}/.env"
    set +a
fi

log "=== Monthly OpenFoodFacts refresh starting ==="

# --- Step 1: cheap freshness check ---
NEW_ETAG=$(curl -sI -L "$OFF_URL" | grep -i '^x-linked-etag:' | tr -d '\r' | awk '{print $2}' || true)
OLD_ETAG=""
[ -f "$STATE_FILE" ] && OLD_ETAG=$(cat "$STATE_FILE")

if [ -n "$NEW_ETAG" ] && [ "$NEW_ETAG" = "$OLD_ETAG" ]; then
    log "No change since last run (ETag unchanged) — skipping download and export."
    exit 0
fi

if [ -z "$NEW_ETAG" ]; then
    log "WARNING: could not read ETag from HEAD response, proceeding with download anyway."
fi

# --- Step 2: throttled download to a temp file ---
log "Downloading fresh export (rate-limited to ${DOWNLOAD_RATE_LIMIT}/s) -> ${TMP_PATH}"
if ! curl -sL --limit-rate "$DOWNLOAD_RATE_LIMIT" -o "$TMP_PATH" "$OFF_URL"; then
    log "ERROR: download failed. Live file untouched, nothing changed."
    rm -f "$TMP_PATH"
    exit 1
fi

DOWNLOADED_SIZE=$(stat -c%s "$TMP_PATH" 2>/dev/null || stat -f%z "$TMP_PATH")
if [ "$DOWNLOADED_SIZE" -lt 1000000000 ]; then
    log "ERROR: downloaded file suspiciously small (${DOWNLOADED_SIZE} bytes) — aborting, live file untouched."
    rm -f "$TMP_PATH"
    exit 1
fi
log "Download complete: ${DOWNLOADED_SIZE} bytes"

# --- Step 3: atomic swap — only now does off-api's live file change ---
mv "$TMP_PATH" "$PARQUET_PATH"
[ -n "$NEW_ETAG" ] && echo "$NEW_ETAG" > "$STATE_FILE"
log "Swapped in new food.parquet"

# --- Step 4: export the lean catalog CSV the business VPS pulls ---
log "Exporting catalog CSV"
python3 "${REPO_DIR}/refresh/export_catalog_csv.py" \
    --parquet "$PARQUET_PATH" \
    --out "$CATALOG_CSV" \
    2>&1 | tee -a "$LOG"

# --- Step 5: rebuild the indexed DB api/app/lookup.py actually queries ---
log "Building indexed food_index.duckdb"
python3 "${REPO_DIR}/refresh/build_index.py" \
    --parquet "$PARQUET_PATH" \
    --out "$INDEX_DB_PATH" \
    2>&1 | tee -a "$LOG"

log "=== Monthly OpenFoodFacts refresh complete ==="

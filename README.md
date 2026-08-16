# OpenFoodFacts Catalog Service

Hosts the full OpenFoodFacts product database on the shared VPS and serves it
to `POS_System_For_all_businesses` (on the business VPS), which no longer
carries the multi-GB Parquet file or the DuckDB scan itself.

## Why this split

The business VPS is tight on resources (2 vCPU / ~3.8GB RAM, already close to
its RAM ceiling running Postgres/Redis/Celery/multiple app containers). The
shared VPS has real headroom (4 vCPU / ~7.8GB RAM, effectively unlimited
disk). Everything CPU/memory/disk-heavy about OpenFoodFacts — the ~7.7GB
Parquet download, the monthly DuckDB re-scan — moves here. The business VPS
keeps only what has to stay local: its own `global_product_catalog` Postgres
table (the fast path checked on every barcode scan) and a lightweight import
script.

## Two independent pieces

1. **Live lookup API** (`api/`) — a small FastAPI service. When the business
   VPS's fast path misses (a barcode not yet cached locally), it calls
   `GET /lookup/{barcode}` here instead of scanning a local file. Backed by
   the same DuckDB-over-Parquet query the business VPS used to run itself.
2. **Monthly refresh** (`refresh/`) — a cron job, run directly on the shared
   VPS host (not in a container, so its memory use never counts against the
   API container's limit). Downloads OpenFoodFacts' latest export (skipped
   entirely if unchanged, via ETag), swaps it in atomically, then exports a
   lean `catalog-latest.csv` (barcode/name/brand/category only — no images,
   no full product blob) that the business VPS pulls over SFTP and imports
   into its own Postgres with the existing `off_refresh_import.py --csv`
   (add-only, never touches a row that already exists).

Product **images are never mirrored here** — both this service's lookup
results and the exported CSV only ever carry an `image_url` pointing at
OpenFoodFacts' own CDN (`images.openfoodfacts.org`). Mirroring images would
be hundreds of GB to TBs; letting the POS frontend load them directly from
OFF's CDN keeps this whole setup at roughly the size of one Parquet file
(~8-10GB with indexes/working files).

## Layout

```
api/                      # FastAPI lookup service (Docker)
  app/
    main.py                 # GET /health (no auth), GET /lookup/{barcode} (API key)
    lookup.py                # DuckDB query — ported verbatim from the business
                              #   VPS's original openfoodfacts_lookup.py, same
                              #   cleaning rules and image-URL derivation
    auth.py                  # X-API-Key check
    config.py
  Dockerfile
  requirements.txt
refresh/                  # Host cron, NOT part of the Docker stack
  off-monthly-refresh.sh    # download check -> throttled download -> atomic
                              #   swap -> export catalog-latest.csv
  export_catalog_csv.py     # Parquet -> CSV (same shape off_refresh_import.py
                              #   --csv already knows how to import)
  requirements.txt          # just duckdb, installed directly on the host
docker-compose.yml         # off-api + cloudflared
.env.example
data/                      # gitignored — food.parquet + working files live here
logs/                      # gitignored
```

## Deployment (run these on the shared VPS — not done by this session)

```bash
cp .env.example .env
# fill in OFF_SERVICE_API_KEY (generate a long random value) and
# CLOUDFLARE_TUNNEL_TOKEN (Cloudflare dashboard -> Zero Trust -> Networks ->
# Tunnels -> Create a tunnel -> Docker -> copy the token). Set the tunnel's
# Public Hostname route to http://off-api:8000.

docker compose up -d --build

# One-time initial Parquet load + first catalog export (this alone can take
# a while — ~8GB download plus a full DuckDB scan):
pip install -r refresh/requirements.txt   # or a venv, host Python
sh refresh/off-monthly-refresh.sh

# Cron (monthly is enough — OpenFoodFacts only regenerates their full export
# monthly upstream; the ETag check makes any extra runs a no-op anyway):
# crontab -e
#   0 4 1 * * cd /path/to/OpenFoodFacts-Catalog-Service && nice -n 19 ionice -c3 sh refresh/off-monthly-refresh.sh
```

Then on the business VPS's `POS_System_For_all_businesses/.env`, set:

```
OFF_SERVICE_URL=https://<your-tunnel-hostname>
OFF_SERVICE_API_KEY=<same value as OFF_SERVICE_API_KEY above>
```

### SFTP access for the business VPS's pull

`refresh/off-monthly-refresh.sh` publishes the finished CSV to
`OFF_CATALOG_PUBLISH_DIR` (defaults to `~/off-catalog/catalog-latest.csv`,
under the `appbox` account's home). The business VPS already has an SSH key
for this account (`backup-ssh-key/id_sharedvps` in the
`POS_System_For_all_businesses` repo, used today for offsite Postgres
backups) — no new credential is needed, it just now also reads from
`~/off-catalog/` in addition to writing to `~/backups/`. That account is
already SFTP-only with no shell, so this doesn't widen its access in any way
that matters.

## Security notes

- `off-api` is not exposed on a public port — only reachable through the
  Cloudflare Tunnel, and every `/lookup/*` request still requires the
  `X-API-Key` header regardless.
- `/health` is intentionally unauthenticated (Docker healthcheck + Cloudflare
  Tunnel origin check need it) but leaks nothing beyond "is the Parquet file
  present."
- Never returns tenant data, price, SKU, or stock — this service only ever
  knows about the public OpenFoodFacts catalog.

#!/usr/bin/env bash
set -euo pipefail

cd ~/OpenFoodFacts-Catalog-Service

echo "==> Syncing to latest master"
# Force-sync tracked files to origin/master instead of a plain `git pull`, same
# reasoning as POS_System_For_all_businesses's deploy.sh: this runs
# non-interactively over SSH from CI, so anything short of a hard reset can
# leave it stuck on local drift. Never touches untracked files, so .env and
# data/ (the Parquet file) are always safe.
git fetch origin master
git reset --hard origin/master

echo "==> Rebuilding and restarting off-api (cloudflared only restarts if changed)"
docker compose up -d --build --remove-orphans

echo "==> Waiting for off-api to be healthy"
for i in $(seq 1 30); do
  status=$(docker inspect off_catalog_api --format '{{.State.Health.Status}}' 2>/dev/null || echo "starting")
  if [ "$status" = "healthy" ]; then
    break
  fi
  sleep 2
done

echo "==> Deploy complete"
docker compose ps

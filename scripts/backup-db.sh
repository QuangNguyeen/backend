#!/usr/bin/env bash
# DictaLearn — nightly Postgres backup to off-site (R2/B2 via rclone remote 'backup').
# Cron: 0 3 * * * /home/deploy/app/scripts/backup-db.sh >> ~/backup.log 2>&1
set -e

cd "$(dirname "$0")/.."
TS=$(date +%F)
COMPOSE="docker compose -f docker-compose.prod.yml"

$COMPOSE exec -T db pg_dump -U "${POSTGRES_USER:-dictalearn}" "${POSTGRES_DB:-dictalearn}" \
    | gzip > "/tmp/db-$TS.sql.gz"

# Push to off-site bucket (configure: rclone config -> remote named 'backup')
rclone copy "/tmp/db-$TS.sql.gz" backup:dictalearn-backups/
rm -f "/tmp/db-$TS.sql.gz"

# Keep 14 days off-site
rclone delete --min-age 14d backup:dictalearn-backups/

echo "Backup db-$TS.sql.gz uploaded."
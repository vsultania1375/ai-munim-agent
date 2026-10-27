#!/usr/bin/env bash
# Nightly backup: every ERPNext site DB + bot state + pool keys -> one encrypted archive
# -> Oracle Object Storage through a write-only pre-authenticated URL.
# Objects are named by weekday, so the bucket always holds the last 7 days and nothing
# ever needs deleting (the URL cannot read or delete anyway).
#
# Needs: /opt/aimunim/backup_par.txt (PAR URL), /opt/aimunim/backup.key (passphrase).
# Restore: openssl enc -d -aes-256-cbc -pbkdf2 -pass file:backup.key -in X.tar.gz.enc | tar xz
set -euo pipefail
cd /opt/aimunim
PAR="$(cat backup_par.txt)"
DAY="$(date +%a)"
WORK="$(mktemp -d)"
trap 'sudo rm -rf "$WORK"' EXIT
mkdir -p "$WORK/erpnext" "$WORK/bot"

# 1) ERPNext: a fresh SQL dump per site (no files; drafts/PDFs regenerate from the DB)
for site in $(sudo docker exec erpnext-backend-1 bash -lc "cd /home/frappe/frappe-bench/sites && ls -d */ | tr -d / | grep -v -e assets -e '^_'"); do
  sudo docker exec erpnext-backend-1 bash -lc "cd /home/frappe/frappe-bench && bench --site $site backup --compress >/dev/null 2>&1 || true"
  latest="$(sudo docker exec erpnext-backend-1 bash -lc "ls -t /home/frappe/frappe-bench/sites/$site/private/backups/*-database.sql.gz 2>/dev/null | head -1")"
  [ -n "$latest" ] && sudo docker cp "erpnext-backend-1:$latest" "$WORK/erpnext/$site.sql.gz"
done

# 2) Bot state: consistent SQLite copies taken inside the container
sudo docker exec -i aimunim-bot-1 python - <<'PY'
import sqlite3, pathlib
out = pathlib.Path("/tmp/statebak"); out.mkdir(exist_ok=True)
for db in pathlib.Path("/app/.run/v2").glob("*.db"):
    src = sqlite3.connect(db); dst = sqlite3.connect(out / db.name)
    src.backup(dst); dst.close(); src.close()
PY
sudo docker cp aimunim-bot-1:/tmp/statebak/. "$WORK/bot/"
sudo docker exec aimunim-bot-1 rm -rf /tmp/statebak

# 3) Config needed to rebuild (secrets are inside the encrypted archive only)
sudo cp -r pool "$WORK/pool"
sudo cp bot.env secrets.env "$WORK/" 2>/dev/null || true

ARCHIVE="$WORK/aimunim-backup-$DAY.tar.gz"
sudo tar -czf "$ARCHIVE" -C "$WORK" erpnext bot pool bot.env secrets.env 2>/dev/null || sudo tar -czf "$ARCHIVE" -C "$WORK" erpnext bot pool
sudo openssl enc -aes-256-cbc -pbkdf2 -salt -pass file:/opt/aimunim/backup.key -in "$ARCHIVE" -out "$ARCHIVE.enc"
curl -sf -X PUT --data-binary @"$ARCHIVE.enc" "${PAR}aimunim-backup-$DAY.tar.gz.enc" >/dev/null
echo "$(date -Is) backup ok: aimunim-backup-$DAY.tar.gz.enc ($(du -h "$ARCHIVE.enc" | cut -f1))"

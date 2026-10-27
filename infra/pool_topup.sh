#!/usr/bin/env bash
# Hourly (cron): keep at least MIN_FREE empty ERPNext sites ready, so a new business never waits for
# `bench new-site` (3-5 minutes). The bot picks new pool files up by itself (Provisioner.refresh).
set -uo pipefail
cd /opt/aimunim
MIN_FREE="${MIN_FREE:-2}"
ADD="${ADD:-2}"
DOMAIN="${MUNIM_PUBLIC_DOMAIN:-aimunim.duckdns.org}"

free=$(curl -sf -m 10 "https://$DOMAIN/health" | python3 -c "import sys, json; print(json.load(sys.stdin).get('free_sites', ''))" 2>/dev/null)
[ -z "$free" ] && { echo "$(date -Is) health unreadable, skipped"; exit 0; }
[ "$free" -ge "$MIN_FREE" ] && exit 0

total=$(sudo sh -c 'ls pool/tenant-*.json 2>/dev/null' | wc -l)
echo "$(date -Is) free sites $free < $MIN_FREE: growing pool from $total to $((total + ADD))"
bash /opt/aimunim/repo/infra/make_pool.sh "$((total + ADD))"

#!/usr/bin/env bash
# Every 5 minutes (cron, root). Checks the bot, the ERP, disk, memory and last night's backup.
# - Bot down twice in a row: restart it.
# - Any problem: WhatsApp the operator, at most once per problem every 6 hours. WhatsApp only delivers
#   free-form messages within 24 h of the operator's last message to the bot, so this is best-effort;
#   an external monitor on https://<domain>/health is the backstop.
set -uo pipefail
cd /opt/aimunim
DOMAIN="${MUNIM_PUBLIC_DOMAIN:-aimunim.duckdns.org}"
problems=()

alert() {  # alert <key> <message>
  local stamp="/opt/aimunim/.alert_$1"
  if [ -f "$stamp" ] && [ $(( $(date +%s) - $(stat -c %Y "$stamp") )) -lt 21600 ]; then return; fi
  touch "$stamp"
  set -a; . ./bot.env; set +a
  local operator="${MUNIM_V2_TESTERS%%,*}"
  if [ -n "$operator" ] && [ -n "${WA_ACCESS_TOKEN:-}" ]; then
    curl -s -m 15 -X POST "https://graph.facebook.com/${WA_GRAPH_API_VERSION:-v25.0}/$WA_PHONE_NUMBER_ID/messages" \
      -H "Authorization: Bearer $WA_ACCESS_TOKEN" -H "Content-Type: application/json" \
      -d "{\"messaging_product\":\"whatsapp\",\"to\":\"$operator\",\"type\":\"text\",\"text\":{\"body\":\"⚠️ AI Munim: $2 ($(date '+%H:%M %d %b'))\"}}" >/dev/null
  fi
  echo "$(date -Is) ALERT $1: $2"
}

# 1. the bot
STATE=/opt/aimunim/.health_failures
if curl -sf -m 10 "https://$DOMAIN/health" >/dev/null; then
  echo 0 > "$STATE"
else
  fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
  echo "$fails" > "$STATE"
  if [ "$fails" -ge 2 ]; then
    docker restart aimunim-bot-1 >/dev/null 2>&1
    alert bot "bot health check failed $fails times; restarted"
  fi
fi

# 2. the ERP (demo site answers for the whole bench)
curl -sf -m 15 "https://demo.$DOMAIN/api/method/ping" >/dev/null || alert erp "ERPNext is not answering"

# 3. disk and memory
disk=$(df --output=pcent / | tail -1 | tr -dc '0-9')
[ "${disk:-0}" -ge 85 ] && alert disk "disk ${disk}% full"
avail=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
[ "${avail:-9999}" -lt 600 ] && alert memory "only ${avail} MB memory free"

# 4. last night's backup (runs 21:30 UTC; allow 26 h)
last=$(grep "backup ok" /var/log/aimunim-backup.log 2>/dev/null | tail -1 | cut -c1-25)
if [ -z "$last" ] || [ $(( $(date +%s) - $(date -d "$last" +%s 2>/dev/null || echo 0) )) -gt 93600 ]; then
  alert backup "no successful backup in the last 26 hours"
fi
exit 0

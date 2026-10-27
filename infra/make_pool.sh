#!/usr/bin/env bash
# Pre-create empty ERPNext sites for new businesses (setup wizard NOT run yet).
# Each site gets an Administrator API key written to /opt/aimunim/pool/<site>.json (0600).
# Usage: bash make_pool.sh 3
set -euo pipefail
COUNT="${1:-3}"
cd /opt/aimunim
. ./secrets.env
sudo mkdir -p pool && sudo chmod 700 pool && sudo chown 1000:1000 pool

for n in $(seq 1 "$COUNT"); do  # tenant-1, tenant-2, ... (no zero padding: names must stay stable)
  site="tenant-$n"
  if sudo test -f "pool/$site.json"; then echo "$site exists"; continue; fi
  admin_pw="$(openssl rand -hex 12)"
  sudo docker exec erpnext-backend-1 bash -lc "cd /home/frappe/frappe-bench && bench new-site $site \
    --mariadb-user-host-login-scope='%' --admin-password '$admin_pw' --db-root-username root \
    --db-root-password '$DB_ROOT_PASSWORD' --install-app erpnext --install-app india_compliance && bench --site $site set-config host_name http://frontend:8080" >/dev/null
  cat > /tmp/mkkey_$site.py <<EOF
import json, frappe
frappe.init(site="$site", sites_path="."); frappe.connect(); frappe.set_user("Administrator")
u = frappe.get_doc("User", "Administrator")
key = frappe.generate_hash(length=15); secret = frappe.generate_hash(length=15)
u.api_key = key; u.api_secret = secret; u.save(); frappe.db.commit()
open("/tmp/$site.json", "w").write(json.dumps({"site": "$site", "api_key": key, "api_secret": secret}))
EOF
  sudo docker cp "/tmp/mkkey_$site.py" "erpnext-backend-1:/tmp/mkkey_$site.py"
  sudo docker exec erpnext-backend-1 bash -lc "cd /home/frappe/frappe-bench/sites && ../env/bin/python /tmp/mkkey_$site.py" 2>/dev/null
  sudo docker cp "erpnext-backend-1:/tmp/$site.json" "pool/$site.json"
  sudo docker exec -u root erpnext-backend-1 rm -f "/tmp/$site.json" "/tmp/mkkey_$site.py"
  rm -f "/tmp/mkkey_$site.py"
  sudo chmod 600 "pool/$site.json" && sudo chown 1000:1000 "pool/$site.json"  # bot container user
  echo "$site ready"
done

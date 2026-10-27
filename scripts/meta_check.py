"""Read-only Meta WhatsApp diagnostics from a local .env. Never prints secrets.

Usage: python .run/meta_check.py [path/to/.env]
"""
import sys
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8")

env_path = Path(sys.argv[1] if len(sys.argv) > 1 else ".env")
env = {}
for line in env_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    env[key.strip()] = value.split(" #")[0].strip().strip('"').strip("'")

token = env.get("WA_ACCESS_TOKEN", "")
phone_id = env.get("WA_PHONE_NUMBER_ID", "")
waba_id = env.get("WA_BUSINESS_ACCOUNT_ID") or env.get("MUNIM_WABA_ID") or env.get("WA_WABA_ID", "")
app_secret = env.get("WA_APP_SECRET", "")
version = env.get("WA_GRAPH_API_VERSION") or "v21.0"
graph = f"https://graph.facebook.com/{version}"

print(f"env file: {env_path.resolve()}")
print(f"token present: {bool(token)} (len {len(token)})")
print(f"phone number id: {phone_id or 'MISSING'}")
print(f"waba id: {waba_id or 'MISSING'}")
print(f"app secret present: {bool(app_secret)}")
print(f"base url in env: {env.get('MUNIM_BASE_URL', '-')}")
print()


def get(path, params, auth=None):
    params = dict(params)
    params["access_token"] = auth or token
    try:
        r = httpx.get(f"{graph}/{path}", params=params, timeout=20)
        return r.status_code, r.json()
    except (httpx.HTTPError, ValueError) as exc:  # network errors or non-JSON body
        return 0, {"error": {"message": type(exc).__name__}}


def show_error(label, body):
    err = body.get("error", {})
    print(f"{label}: FAILED - {err.get('type', '')} {err.get('code', '')} {err.get('message', '')}")


if not token:
    sys.exit("No WA_ACCESS_TOKEN in env file.")

app_id = ""
status, body = get("debug_token", {"input_token": token})
if status == 200 and "data" in body:
    d = body["data"]
    app_id = str(d.get("app_id", ""))
    print("== Token ==")
    print(f"valid: {d.get('is_valid')}")
    print(f"type: {d.get('type')}")
    print(f"app: {d.get('application')} (app id {app_id})")
    exp = d.get("expires_at")
    print(f"expires_at: {'never' if exp in (0, None) else exp}")
    print(f"scopes: {', '.join(d.get('scopes', []))}")
    for g in d.get("granular_scopes", []):
        print(f"  {g.get('scope')}: {g.get('target_ids', 'all')}")
else:
    show_error("debug_token", body)
print()

if phone_id:
    status, body = get(phone_id, {"fields": "display_phone_number,verified_name,name_status,quality_rating,code_verification_status,platform_type,status"})
    print("== Phone number ==")
    if status == 200:
        for k, v in body.items():
            if k != "id":
                print(f"{k}: {v}")
    else:
        show_error("phone", body)
    print()

if waba_id:
    status, body = get(waba_id, {"fields": "name,owner_business_info,account_review_status,business_verification_status"})
    print("== WhatsApp Business Account ==")
    if status == 200:
        for k, v in body.items():
            if k != "id":
                print(f"{k}: {v}")
    else:
        show_error("waba", body)
    status, body = get(f"{waba_id}/subscribed_apps", {})
    if status == 200:
        apps = [a.get("whatsapp_business_api_data", {}) for a in body.get("data", [])]
        print(f"subscribed apps: {[(a.get('name'), a.get('id')) for a in apps]}")
        for a in body.get("data", []):
            if a.get("override_callback_uri"):
                print(f"override callback: {a['override_callback_uri']}")
    else:
        show_error("subscribed_apps", body)
    print()

if app_id and app_secret:
    status, body = get(f"{app_id}/subscriptions", {}, auth=f"{app_id}|{app_secret}")
    print("== App webhook subscriptions ==")
    if status == 200:
        for s in body.get("data", []):
            fields = [f.get("name") if isinstance(f, dict) else f for f in s.get("fields", [])]
            print(f"object={s.get('object')} active={s.get('active')} callback={s.get('callback_url')}")
            print(f"  fields: {fields}")
    else:
        show_error("app subscriptions (app secret may not match this app)", body)

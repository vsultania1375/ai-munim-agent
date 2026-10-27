"""Open a Cloudflare quick tunnel to the local v2 server and point the Meta webhook at it.

Quick-tunnel URLs change on every start, so the webhook is re-registered each time
with the app access token (APP_ID|APP_SECRET). Secrets are read from .env and never
printed.

Usage: python scripts/v2_tunnel.py [--port 8800]
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8")
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.split(" #")[0].strip().strip('"').strip("'")
    return env


def app_id_for(env: dict[str, str], graph: str) -> str:
    if env.get("WA_APP_ID"):
        return env["WA_APP_ID"]
    token = env["WA_ACCESS_TOKEN"]
    data = httpx.get(f"{graph}/debug_token", params={"input_token": token, "access_token": token}, timeout=20).json()
    return str(data["data"]["app_id"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()

    env = read_env(Path(args.env))
    graph = f"https://graph.facebook.com/{env.get('WA_GRAPH_API_VERSION') or 'v25.0'}"
    health = f"http://127.0.0.1:{args.port}/health"
    try:
        httpx.get(health, timeout=5).raise_for_status()
    except httpx.HTTPError:
        print(f"Local server is not running on {health}. Start: python -m munim.core.server")
        return 1

    # An existing ~/.cloudflared/config.yml (named tunnel ingress) would override --url
    # and answer 404, so run the quick tunnel with an empty config of its own.
    empty_config = Path(".run/cloudflared-quick.yml")
    empty_config.parent.mkdir(parents=True, exist_ok=True)
    empty_config.write_text("# quick tunnel: no ingress rules\n", encoding="utf-8")
    proc = subprocess.Popen(
        [
            "cloudflared", "tunnel", "--config", str(empty_config), "--no-autoupdate",
            "--url", f"http://127.0.0.1:{args.port}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    public = None
    deadline = time.time() + 60
    assert proc.stdout is not None
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.2)
            continue
        found = URL_RE.search(line)
        if found:
            public = found.group(0)
            break
    if not public:
        print("Could not get a tunnel URL from cloudflared.")
        proc.terminate()
        return 1
    print(f"Tunnel: {public}")

    for _ in range(30):  # DNS for a fresh quick tunnel takes a few seconds
        try:
            if httpx.get(f"{public}/health", timeout=5).status_code == 200:
                break
        except httpx.HTTPError:  # tunnel DNS not ready yet
            pass
        time.sleep(2)

    app_id = app_id_for(env, graph)
    resp = httpx.post(
        f"{graph}/{app_id}/subscriptions",
        params={
            "object": "whatsapp_business_account",
            "callback_url": f"{public}/wa/webhook",
            "verify_token": env["WA_VERIFY_TOKEN"],
            "fields": "messages",
            "access_token": f"{app_id}|{env['WA_APP_SECRET']}",
        },
        timeout=30,
    )
    ok = resp.status_code == 200 and resp.json().get("success")
    print("Meta webhook updated." if ok else f"Meta webhook update failed: {resp.status_code} {resp.text[:300]}")
    print("Tunnel running. Ctrl+C to stop.", flush=True)
    try:
        for line in proc.stdout:
            if "ERR" in line:
                print(line.rstrip(), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

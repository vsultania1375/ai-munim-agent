#!/usr/bin/env bash
# Download every wheel the bot image needs into ./wheels, retrying until done. pip skips files it already has,
# so a slow or broken connection only costs the file that failed. Then the image builds from ./wheels.
#   bash infra/fetch_wheels.sh            (run from the repo root on the VM)
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p wheels
for attempt in 1 2 3 4 5 6 7 8; do
  if sudo docker run --rm --network host -v "$PWD:/src:ro" -v "$PWD/wheels:/wheels" python:3.12-slim \
      bash -c 'mkdir -p /tmp/p/munim && cp /src/pyproject.toml /tmp/p/ && touch /tmp/p/munim/__init__.py \n        && pip download --quiet --timeout 120 --retries 3 --dest /wheels "/tmp/p[voice]"'; then
    echo "wheels ready ($(ls wheels | wc -l) files)"
    exit 0
  fi
  echo "attempt $attempt failed, retrying"
  sleep 10
done
exit 1

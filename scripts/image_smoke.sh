#!/usr/bin/env bash
# Smoke-test a built image under the same constraints the Helm chart applies:
# read-only root filesystem, writable /tmp only, UID 10001.
#
# Usage: scripts/image_smoke.sh [image]   (default: systemone-gemini-gateway:dev)
set -euo pipefail

IMAGE="${1:-systemone-gemini-gateway:dev}"
MAX_IMAGE_BYTES=$((200 * 1000 * 1000))
NAME="s1gw-smoke-$$"
STARTED=0

cleanup() {
  local rc=$?
  if [ "$STARTED" = 1 ]; then
    if [ "$rc" -ne 0 ]; then
      echo "--- docker logs ${NAME} ---" >&2
      docker logs "$NAME" >&2 || true
    fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
  fi
  return "$rc"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# Assert that GET <url> returns HTTP 200; prints the body to stdout.
get_200() {
  local url="$1" body code
  body="$(mktemp)"
  code="$(curl -sS -o "$body" -w '%{http_code}' --max-time 5 "$url")" || {
    rm -f "$body"
    fail "request to ${url} failed"
  }
  if [ "$code" != 200 ]; then
    rm -f "$body"
    fail "GET ${url} returned ${code}, expected 200"
  fi
  cat "$body"
  rm -f "$body"
}

echo "==> image size check: ${IMAGE}"
size="$(docker image inspect -f '{{.Size}}' "$IMAGE")"
echo "    ${size} bytes (limit ${MAX_IMAGE_BYTES})"
[ "$size" -lt "$MAX_IMAGE_BYTES" ] || fail "image is ${size} bytes, limit is ${MAX_IMAGE_BYTES} (200 MB)"

echo "==> starting container"
docker run -d --name "$NAME" \
  --read-only --tmpfs /tmp \
  --user 10001:10001 \
  -e GEMINI_API_KEY=dummy \
  -e OTEL_METRICS_EXPORTER=prometheus \
  -p 127.0.0.1::8080 -p 127.0.0.1::9464 \
  "$IMAGE" >/dev/null
STARTED=1

api_port="$(docker port "$NAME" 8080/tcp | head -n1 | sed 's/.*://')"
metrics_port="$(docker port "$NAME" 9464/tcp | head -n1 | sed 's/.*://')"
[ -n "$api_port" ] && [ -n "$metrics_port" ] || fail "could not determine published ports"
api="http://127.0.0.1:${api_port}"
metrics="http://127.0.0.1:${metrics_port}"

echo "==> waiting for ${api}/healthz (up to 15s)"
ready=0
for _ in $(seq 1 30); do
  if [ "$(docker inspect -f '{{.State.Running}}' "$NAME")" != true ]; then
    fail "container exited before becoming healthy"
  fi
  if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "${api}/healthz" || true)" = 200 ]; then
    ready=1
    break
  fi
  sleep 0.5
done
[ "$ready" = 1 ] || fail "/healthz did not return 200 within 15s"

echo "==> /readyz"
get_200 "${api}/readyz" >/dev/null

echo "==> /v1/models"
models="$(get_200 "${api}/v1/models")"
printf '%s' "$models" | python3 -c '
import json, sys
body = json.load(sys.stdin)
items = body.get("data") if isinstance(body, dict) else body
if not isinstance(items, list):
    sys.exit("expected a JSON list or an object with a data list, got: %r" % (body,))
' || fail "/v1/models did not return the expected JSON shape"

echo "==> /metrics on 9464"
metrics_body="$(get_200 "${metrics}/metrics")"
if ! printf '%s' "$metrics_body" | grep -Eq 'target_info|s1gw_'; then
  fail "/metrics contains neither target_info nor s1gw_"
fi

echo "==> process uid"
uid="$(docker exec "$NAME" id -u)"
[ "$uid" = 10001 ] || fail "container runs as uid ${uid}, expected 10001"

echo "OK: ${IMAGE} passed the smoke test"

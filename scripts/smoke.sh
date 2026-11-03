#!/usr/bin/env bash
# Smoke test a deployed environment:
#   1. kubectl port-forward to every Service and GET its health path (always);
#   2. when INGRESS_HOST is set, go through ingress-nginx on localhost:80 and check that
#      public routes answer and internal services are not exposed.
set -euo pipefail

NAMESPACE="${NAMESPACE:-pulso}"
INGRESS_HOST="${INGRESS_HOST:-}"
# service:local-port:readiness-path, as served by each service repository
SERVICES=("orders-api:13000:/health/ready" "matching:18000:/health" "support-agents:18080:/healthz" "saga:18090:/health")

pids=()
cleanup() {
  for pid in "${pids[@]+"${pids[@]}"}"; do kill "${pid}" 2>/dev/null || true; done
}
trap cleanup EXIT

wait_for_200() {
  local url="$1"
  shift
  for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 "$@" "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

failures=0
for entry in "${SERVICES[@]}"; do
  IFS=: read -r svc port path <<<"${entry}"
  kubectl -n "${NAMESPACE}" port-forward "svc/${svc}" "${port}:80" >/dev/null 2>&1 &
  pids+=("$!")
  if wait_for_200 "http://127.0.0.1:${port}${path}"; then
    echo "ok    svc/${svc} ${path}"
  else
    echo "FAIL  svc/${svc} ${path}" >&2
    failures=$((failures + 1))
  fi
done

if [[ -n "${INGRESS_HOST}" ]]; then
  for route in /api/orders/health/ready /api/support/healthz; do
    if wait_for_200 "http://127.0.0.1${route}" -H "Host: ${INGRESS_HOST}"; then
      echo "ok    ingress ${INGRESS_HOST}${route}"
    else
      echo "FAIL  ingress ${INGRESS_HOST}${route}" >&2
      failures=$((failures + 1))
    fi
  done
  status="$(curl -s -o /dev/null -w '%{http_code}' -H "Host: ${INGRESS_HOST}" http://127.0.0.1/api/orders/metrics)"
  if [[ "${status}" == "404" ]]; then
    echo "ok    ingress does not route orders-api /metrics (404)"
  else
    echo "FAIL  ingress answered ${status} for /api/orders/metrics (metrics exposed publicly?)" >&2
    failures=$((failures + 1))
  fi
  status="$(curl -s -o /dev/null -w '%{http_code}' -H "Host: ${INGRESS_HOST}" http://127.0.0.1/api/matching/health)"
  if [[ "${status}" == "404" ]]; then
    echo "ok    ingress does not expose matching (404)"
  else
    echo "FAIL  ingress answered ${status} for /api/matching (internal service exposed?)" >&2
    failures=$((failures + 1))
  fi
fi

if ((failures > 0)); then
  echo "smoke: ${failures} check(s) failed" >&2
  exit 1
fi
echo "smoke: all checks passed"

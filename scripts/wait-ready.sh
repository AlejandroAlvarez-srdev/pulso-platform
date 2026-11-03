#!/usr/bin/env bash
# Block until the data stores and every Deployment in the namespace are ready.
set -euo pipefail

NAMESPACE="${NAMESPACE:-pulso}"
TIMEOUT="${TIMEOUT:-240s}"

for sts in $(kubectl -n "${NAMESPACE}" get statefulsets -o name); do
  kubectl -n "${NAMESPACE}" rollout status "${sts}" --timeout="${TIMEOUT}"
done
for deploy in $(kubectl -n "${NAMESPACE}" get deployments -o name); do
  kubectl -n "${NAMESPACE}" rollout status "${deploy}" --timeout="${TIMEOUT}"
done
kubectl -n "${NAMESPACE}" get pods -o wide
